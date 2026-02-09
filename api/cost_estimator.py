"""Cost estimation for log-to-metric conversion.

Compares log storage cost vs metric storage cost to quantify savings
from converting a log aggregation into a pre-materialized metric.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

import es_connector
from models import RuleCreate

# ── Constants ────────────────────────────────────────────────────────

# Approximate size of a single metric point in an ES TSDS index
# (timestamp + dimensions + value, with doc-value compression).
# ES TSDS compresses metric docs heavily — typically 30-50 bytes per point.
METRIC_POINT_SIZE_BYTES = 40

# Default log retention assumption when not specified (days)
DEFAULT_LOG_RETENTION_DAYS = 30

# High-cardinality field names that Datadog warns against grouping by
HIGH_CARDINALITY_FIELDS = {
    "user_id", "userid", "user_name", "username",
    "request_id", "requestid", "req_id",
    "session_id", "sessionid",
    "trace_id", "traceid", "span_id", "spanid",
    "transaction_id", "txn_id",
    "ip", "ip_address", "client_ip", "source_ip",
    "uuid", "guid", "correlation_id",
    "message", "msg", "log", "body",
}


# ── Models ───────────────────────────────────────────────────────────


class CostEstimate(BaseModel):
    log_storage_gb: float
    metric_storage_gb: float
    savings_gb: float
    savings_pct: float
    query_speedup_x: float
    estimated_series_count: int
    docs_per_day: int
    metric_points_per_day: int
    log_retention_days: int
    metric_retention_days: int


# ── Public API ───────────────────────────────────────────────────────


def estimate_cost(
    rule: RuleCreate,
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
) -> CostEstimate:
    """Estimate storage cost comparison between logs and metrics for a rule.

    Fetches live index stats and field cardinalities from ES.
    """
    index = rule.source.index_pattern.rstrip("*")
    if not index:
        index = rule.source.index_pattern

    stats = es_connector.get_index_stats(index)
    doc_count = stats.doc_count
    store_bytes = stats.store_size_bytes

    if doc_count == 0:
        return CostEstimate(
            log_storage_gb=0,
            metric_storage_gb=0,
            savings_gb=0,
            savings_pct=0,
            query_speedup_x=1.0,
            estimated_series_count=0,
            docs_per_day=0,
            metric_points_per_day=0,
            log_retention_days=log_retention_days,
            metric_retention_days=rule.backend_config.retention_days,
        )

    avg_doc_size = store_bytes / doc_count

    # Estimate docs per day (assume index holds ~1 day of data for PoC;
    # in production we'd look at index creation date or date range).
    docs_per_day = doc_count

    # ── Series count ─────────────────────────────────────────────
    series_count = _estimate_series_count(index, rule.group_by.dimensions)

    # ── Bucket math ──────────────────────────────────────────────
    bucket_seconds = _parse_time_bucket_seconds(rule.group_by.time_bucket)
    points_per_day_per_series = 86400 // bucket_seconds
    metric_points_per_day = series_count * points_per_day_per_series

    # ── Storage ──────────────────────────────────────────────────
    log_storage_bytes = docs_per_day * avg_doc_size * log_retention_days
    metric_storage_bytes = (
        metric_points_per_day * METRIC_POINT_SIZE_BYTES
        * rule.backend_config.retention_days
    )

    log_storage_gb = log_storage_bytes / (1024 ** 3)
    metric_storage_gb = metric_storage_bytes / (1024 ** 3)
    savings_gb = log_storage_gb - metric_storage_gb
    savings_pct = (savings_gb / log_storage_gb * 100) if log_storage_gb > 0 else 0

    # ── Query speedup ────────────────────────────────────────────
    if metric_points_per_day > 0:
        query_speedup = docs_per_day / metric_points_per_day
    else:
        query_speedup = 1.0

    return CostEstimate(
        log_storage_gb=round(log_storage_gb, 4),
        metric_storage_gb=round(metric_storage_gb, 4),
        savings_gb=round(savings_gb, 4),
        savings_pct=round(savings_pct, 1),
        query_speedup_x=round(query_speedup, 1),
        estimated_series_count=series_count,
        docs_per_day=docs_per_day,
        metric_points_per_day=metric_points_per_day,
        log_retention_days=log_retention_days,
        metric_retention_days=rule.backend_config.retention_days,
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _estimate_series_count(index: str, dimensions: list[str]) -> int:
    """Estimate the number of unique metric series from dimension cardinalities.

    series_count = product of cardinalities of all dimensions.
    With no dimensions, there's exactly 1 series.
    """
    if not dimensions:
        return 1

    product = 1
    for dim in dimensions:
        try:
            card = es_connector.get_field_cardinality(index, dim)
            product *= max(card.cardinality, 1)
        except Exception:
            # If we can't get cardinality, assume a conservative 100
            product *= 100

    return product


def _parse_time_bucket_seconds(bucket: str) -> int:
    """Parse a time bucket string like '1m', '10s', '5m', '1h' to seconds."""
    if not bucket:
        return 60  # default 1 minute

    try:
        if bucket.endswith("s"):
            return max(1, int(bucket[:-1]))
        if bucket.endswith("m"):
            return max(1, int(bucket[:-1]) * 60)
        if bucket.endswith("h"):
            return max(1, int(bucket[:-1]) * 3600)
        if bucket.endswith("d"):
            return max(1, int(bucket[:-1]) * 86400)
    except (ValueError, IndexError):
        pass

    return 60  # fallback

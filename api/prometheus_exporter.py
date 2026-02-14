"""Prometheus exporter — reads pre-computed metrics from ES indices and
exposes them in Prometheus text format for scraping.

Metrics are collected on each scrape (no background thread). The exporter
queries each active rule's metrics index for recent data and maps ES
documents to Prometheus gauges with dimension labels.
"""

import logging
import re

from elasticsearch import Elasticsearch, NotFoundError
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlmodel import Session, select

from config import ES_URL
from database import engine
from elastic_backend import INDEX_PREFIX, TRANSFORM_PREFIX, backend as metrics_backend
from models import ComputeConfig, ComputeType, GroupByConfig, LogMetricRule, RuleStatus, SourceConfig

log = logging.getLogger(__name__)

# Dedicated registry so we don't mix with prometheus_client default metrics
_registry = CollectorRegistry()

# Health gauges (static names, dynamic labels)
_transform_health = Gauge(
    "l2m_transform_health",
    "Transform health (0=unknown, 1=green, 2=yellow, 3=red, 4=stopped)",
    ["rule_id", "rule_name"],
    registry=_registry,
)
_transform_docs_processed = Gauge(
    "l2m_transform_docs_processed",
    "Total documents processed by transform",
    ["rule_id", "rule_name"],
    registry=_registry,
)
_transform_docs_indexed = Gauge(
    "l2m_transform_docs_indexed",
    "Total documents indexed by transform",
    ["rule_id", "rule_name"],
    registry=_registry,
)

# Per-rule gauges created dynamically (names depend on rule config)
_rule_gauges: dict[str, Gauge] = {}

HEALTH_VALUE = {
    "unknown": 0,
    "green": 1,
    "yellow": 2,
    "red": 3,
    "stopped": 4,
}

# How far back to query each metrics index.  Wide window so we always find
# data even when transforms run infrequently.  Deduplication (keep latest
# value per unique dimension combination) prevents stale inflation.
LOOKBACK = "24h"


def sanitize_metric_name(name: str) -> str:
    """Convert a rule name to a valid Prometheus metric name component.

    Lowercase, replace non-alphanumeric with underscore, collapse runs,
    strip leading/trailing underscores.
    """
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def collect_and_generate() -> bytes:
    """Collect metrics from all active rules, return Prometheus text format.

    Called once per Prometheus scrape. Clears previous per-rule gauges,
    queries ES for each active rule, and repopulates.
    """
    _clear_rule_gauges()

    es = Elasticsearch(ES_URL)
    try:
        with Session(engine) as session:
            rules = session.exec(
                select(LogMetricRule).where(
                    LogMetricRule.status == RuleStatus.active.value
                )
            ).all()

            for rule in rules:
                try:
                    _collect_rule_metrics(es, rule)
                except Exception:
                    log.exception("Failed to collect metrics for rule %d (%s)", rule.id, rule.name)

                try:
                    _collect_transform_health(rule)
                except Exception:
                    log.exception("Failed to collect health for rule %d (%s)", rule.id, rule.name)
    finally:
        es.close()

    return generate_latest(_registry)


# ── Internal helpers ─────────────────────────────────────────────────


def _collect_rule_metrics(es: Elasticsearch, rule: LogMetricRule) -> None:
    """Query a rule's metrics index and populate Prometheus gauges."""
    source = SourceConfig(**rule.source)
    group_by = GroupByConfig(**rule.group_by)
    compute = ComputeConfig(**rule.compute)

    index_name = f"{INDEX_PREFIX}{rule.id}"

    try:
        result = es.search(
            index=index_name,
            body={
                "query": {"range": {source.time_field: {"gte": f"now-{LOOKBACK}"}}},
                "sort": [{source.time_field: {"order": "desc"}}],
                "size": 1000,
            },
        )
    except NotFoundError:
        log.debug("Index %s not found, skipping", index_name)
        return

    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        return

    sanitized = sanitize_metric_name(rule.name)

    # Deduplicate: keep latest value per unique dimension combination
    seen: set[tuple] = set()

    # All per-rule gauges include rule_name as a label so Grafana can
    # use it as a template variable for repeating panels (one panel per rule).
    all_label_names = ["rule_name"] + group_by.dimensions

    for hit in hits:
        doc = hit["_source"]
        labels = {"rule_name": rule.name}
        labels.update({dim: str(doc.get(dim, "unknown")) for dim in group_by.dimensions})
        label_key = tuple(sorted(labels.items()))

        if label_key in seen:
            continue
        seen.add(label_key)

        if compute.type == ComputeType.count:
            _set_gauge(
                f"l2m_rule_{sanitized}_event_count",
                "Event count",
                all_label_names,
                labels,
                doc.get("event_count", 0),
            )

        elif compute.type == ComputeType.sum:
            field_name = sanitize_metric_name(compute.field)
            _set_gauge(
                f"l2m_rule_{sanitized}_sum_{field_name}",
                f"Sum of {compute.field}",
                all_label_names,
                labels,
                doc.get(f"sum_{compute.field}", 0),
            )

        elif compute.type == ComputeType.avg:
            field_name = sanitize_metric_name(compute.field)
            _set_gauge(
                f"l2m_rule_{sanitized}_avg_{field_name}",
                f"Average of {compute.field}",
                all_label_names,
                labels,
                doc.get(f"avg_{compute.field}", 0),
            )

        elif compute.type == ComputeType.distribution:
            pct_data = doc.get(f"pct_{compute.field}", {})
            # ES percentile agg stores values under a "values" key
            values = pct_data.get("values", pct_data) if isinstance(pct_data, dict) else {}
            field_name = sanitize_metric_name(compute.field)
            for pct_key, pct_val in values.items():
                if pct_val is None:
                    continue
                # "50.0" -> "p50", "99.0" -> "p99"
                pct_label = f"p{str(pct_key).split('.')[0]}"
                _set_gauge(
                    f"l2m_rule_{sanitized}_{pct_label}_{field_name}",
                    f"{pct_label} of {compute.field}",
                    all_label_names,
                    labels,
                    pct_val,
                )


def _collect_transform_health(rule: LogMetricRule) -> None:
    """Query transform stats and populate health gauges."""
    status = metrics_backend.get_status(rule.id)
    health_str = status.health.value if hasattr(status.health, "value") else str(status.health)
    health_val = HEALTH_VALUE.get(health_str, 0)

    labels = {"rule_id": str(rule.id), "rule_name": rule.name}
    _transform_health.labels(**labels).set(health_val)
    _transform_docs_processed.labels(**labels).set(status.docs_processed)
    _transform_docs_indexed.labels(**labels).set(status.docs_indexed)


def _set_gauge(name: str, description: str, label_names: list[str],
               labels: dict[str, str], value: float) -> None:
    """Set a value on a per-rule gauge, creating it if needed."""
    if name not in _rule_gauges:
        _rule_gauges[name] = Gauge(name, description, label_names, registry=_registry)
    if label_names:
        _rule_gauges[name].labels(**labels).set(value)
    else:
        _rule_gauges[name].set(value)


def _clear_rule_gauges() -> None:
    """Unregister all per-rule gauges so stale metrics disappear."""
    for name, gauge in list(_rule_gauges.items()):
        try:
            _registry.unregister(gauge)
        except Exception:
            pass
    _rule_gauges.clear()

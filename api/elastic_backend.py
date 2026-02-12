"""ElasticMetricsBackend — provisions ES continuous transforms for LogMetricRules."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from elasticsearch import Elasticsearch, NotFoundError

from backend import (
    BackendStatus,
    MetricsBackend,
    ProvisionResult,
    TransformHealth,
    ValidationResult,
)
from config import ES_URL
from models import (
    BackendConfig,
    ComputeConfig,
    ComputeType,
    GroupByConfig,
    LogMetricRule,
    SourceConfig,
)

log = logging.getLogger(__name__)

es = Elasticsearch(ES_URL)

TRANSFORM_PREFIX = "l2m-rule-"
INDEX_PREFIX = "l2m-metrics-rule-"
ILM_PREFIX = "l2m-metrics-"


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_time_bucket_seconds(bucket: str) -> int:
    """Parse a time bucket string like '1m', '10s', '5m', '1h' to seconds."""
    if not bucket:
        return 60
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
    return 60


def _map_transform_state(state: str) -> TransformHealth:
    """Map ES transform state string to TransformHealth enum."""
    mapping = {
        "started": TransformHealth.green,
        "indexing": TransformHealth.green,
        "stopping": TransformHealth.yellow,
        "stopped": TransformHealth.stopped,
        "aborting": TransformHealth.red,
        "failed": TransformHealth.red,
    }
    return mapping.get(state, TransformHealth.unknown)


def _build_aggregations(compute: ComputeConfig, time_field: str) -> dict:
    """Build the pivot aggregations dict from a ComputeConfig."""
    aggs = {}

    if compute.type == ComputeType.count:
        aggs["doc_count"] = {"value_count": {"field": time_field}}
    elif compute.type == ComputeType.sum:
        aggs[f"sum_{compute.field}"] = {"sum": {"field": compute.field}}
    elif compute.type == ComputeType.avg:
        aggs[f"avg_{compute.field}"] = {"avg": {"field": compute.field}}
    elif compute.type == ComputeType.distribution:
        percents = compute.percentiles or [50.0, 75.0, 90.0, 95.0, 99.0]
        aggs[f"pct_{compute.field}"] = {
            "percentiles": {
                "field": compute.field,
                "percents": percents,
            }
        }

    return aggs


# ── Backend implementation ───────────────────────────────────────────


class ElasticMetricsBackend(MetricsBackend):

    def validate(self, rule: LogMetricRule) -> ValidationResult:
        errors = []
        source = SourceConfig(**rule.source)
        compute = ComputeConfig(**rule.compute)

        # Check source index exists
        if not es.indices.exists(index=source.index_pattern):
            errors.append(f"Source index '{source.index_pattern}' does not exist")

        # Check compute field exists for non-count types
        if compute.type != ComputeType.count and compute.field:
            try:
                mapping = es.indices.get_mapping(index=source.index_pattern)
                props = {}
                for idx_name in mapping:
                    props = mapping[idx_name]["mappings"].get("properties", {})
                    break
                if compute.field not in props:
                    errors.append(
                        f"Compute field '{compute.field}' not found in index"
                    )
            except Exception as e:
                errors.append(f"Could not verify mapping: {e}")

        # Check transform doesn't already exist
        transform_id = f"{TRANSFORM_PREFIX}{rule.id}"
        try:
            es.transform.get_transform(transform_id=transform_id)
            errors.append(f"Transform '{transform_id}' already exists")
        except NotFoundError:
            pass

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def provision(self, rule: LogMetricRule) -> ProvisionResult:
        transform_id = f"{TRANSFORM_PREFIX}{rule.id}"
        dest_index = f"{INDEX_PREFIX}{rule.id}"
        backend_cfg = BackendConfig(**rule.backend_config)

        try:
            # Step 1: ILM policy
            ilm_policy = self._ensure_ilm_policy(backend_cfg.retention_days)

            # Step 2: Metrics index
            self._create_metrics_index(rule, ilm_policy)

            # Step 3: Create transform
            transform_body = self._build_transform_body(rule)
            es.transform.put_transform(
                transform_id=transform_id, body=transform_body, timeout="30s"
            )
            log.info("Created transform %s", transform_id)

            # Step 4: Start transform
            es.transform.start_transform(
                transform_id=transform_id, timeout="30s"
            )
            log.info("Started transform %s", transform_id)

            return ProvisionResult(
                success=True,
                transform_id=transform_id,
                metrics_index=dest_index,
                ilm_policy=ilm_policy,
            )

        except Exception as e:
            log.error("Provisioning failed for rule %s: %s", rule.id, e)
            self._cleanup_partial(transform_id, dest_index)
            return ProvisionResult(
                success=False,
                transform_id=transform_id,
                metrics_index=dest_index,
                error=str(e),
            )

    def get_status(self, rule_id: int) -> BackendStatus:
        transform_id = f"{TRANSFORM_PREFIX}{rule_id}"
        try:
            stats = es.transform.get_transform_stats(transform_id=transform_id)
            transforms = stats.get("transforms", [])
            if not transforms:
                return BackendStatus(
                    rule_id=rule_id,
                    transform_id=transform_id,
                    health=TransformHealth.unknown,
                )

            t = transforms[0]
            state = t.get("state", "unknown")
            health = _map_transform_state(state)

            stats_block = t.get("stats", {})
            docs_processed = stats_block.get("documents_processed", 0)
            docs_indexed = stats_block.get("documents_indexed", 0)

            checkpointing = t.get("checkpointing", {})
            last_cp = checkpointing.get("last", {})
            last_cp_time = last_cp.get("timestamp_millis")
            last_checkpoint_dt = (
                datetime.fromtimestamp(last_cp_time / 1000, tz=timezone.utc)
                if last_cp_time
                else None
            )

            return BackendStatus(
                rule_id=rule_id,
                transform_id=transform_id,
                health=health,
                docs_processed=docs_processed,
                docs_indexed=docs_indexed,
                last_checkpoint=last_checkpoint_dt,
            )

        except NotFoundError:
            return BackendStatus(
                rule_id=rule_id,
                transform_id=transform_id,
                health=TransformHealth.unknown,
                error="Transform not found",
            )

    def deprovision(self, rule_id: int) -> None:
        transform_id = f"{TRANSFORM_PREFIX}{rule_id}"
        index_name = f"{INDEX_PREFIX}{rule_id}"

        # Stop transform
        try:
            es.transform.stop_transform(
                transform_id=transform_id,
                wait_for_completion=True,
                timeout="30s",
                force=True,
            )
            log.info("Stopped transform %s", transform_id)
        except NotFoundError:
            log.info("Transform %s not found (already removed?)", transform_id)
        except Exception as e:
            log.warning("Error stopping transform %s: %s", transform_id, e)

        # Delete transform
        try:
            es.transform.delete_transform(transform_id=transform_id)
            log.info("Deleted transform %s", transform_id)
        except NotFoundError:
            pass
        except Exception as e:
            log.warning("Error deleting transform %s: %s", transform_id, e)

        # Delete metrics index
        try:
            es.indices.delete(index=index_name)
            log.info("Deleted metrics index %s", index_name)
        except NotFoundError:
            pass
        except Exception as e:
            log.warning("Error deleting index %s: %s", index_name, e)

    # ── Private helpers ──────────────────────────────────────────────

    def _ensure_ilm_policy(self, retention_days: int) -> str:
        policy_name = f"{ILM_PREFIX}{retention_days}d"
        try:
            es.ilm.get_lifecycle(name=policy_name)
            log.info("ILM policy %s already exists", policy_name)
        except NotFoundError:
            es.ilm.put_lifecycle(
                name=policy_name,
                body={
                    "policy": {
                        "phases": {
                            "hot": {
                                "actions": {
                                    "rollover": {
                                        "max_age": "30d",
                                        "max_primary_shard_size": "50gb",
                                    }
                                }
                            },
                            "delete": {
                                "min_age": f"{retention_days}d",
                                "actions": {"delete": {}},
                            },
                        }
                    }
                },
            )
            log.info("Created ILM policy %s", policy_name)
        return policy_name

    def _create_metrics_index(self, rule: LogMetricRule, ilm_policy: str) -> str:
        index_name = f"{INDEX_PREFIX}{rule.id}"
        source = SourceConfig(**rule.source)
        group_by = GroupByConfig(**rule.group_by)
        compute = ComputeConfig(**rule.compute)

        properties = {
            source.time_field: {"type": "date"},
        }

        for dim in group_by.dimensions:
            properties[dim] = {"type": "keyword"}

        if compute.type == ComputeType.count:
            properties["doc_count"] = {"type": "long"}
        elif compute.type == ComputeType.sum:
            properties[f"sum_{compute.field}"] = {"type": "double"}
        elif compute.type == ComputeType.avg:
            properties[f"avg_{compute.field}"] = {"type": "double"}
        elif compute.type == ComputeType.distribution:
            properties[f"pct_{compute.field}"] = {"type": "object"}

        es.indices.create(
            index=index_name,
            body={
                "settings": {
                    "index.lifecycle.name": ilm_policy,
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
                "mappings": {"properties": properties},
            },
        )
        log.info("Created metrics index %s", index_name)
        return index_name

    def _build_transform_body(self, rule: LogMetricRule) -> dict:
        source_cfg = SourceConfig(**rule.source)
        group_by_cfg = GroupByConfig(**rule.group_by)
        compute_cfg = ComputeConfig(**rule.compute)

        # Source block
        source_block = {"index": [source_cfg.index_pattern]}
        if source_cfg.filter_query:
            source_block["query"] = source_cfg.filter_query
        else:
            source_block["query"] = {"match_all": {}}

        # Group-by block
        group_by = {
            source_cfg.time_field: {
                "date_histogram": {
                    "field": source_cfg.time_field,
                    "fixed_interval": group_by_cfg.time_bucket,
                }
            }
        }
        for dim in group_by_cfg.dimensions:
            group_by[dim] = {"terms": {"field": dim}}

        # Aggregations
        aggregations = _build_aggregations(compute_cfg, source_cfg.time_field)

        # Sync block (continuous transform)
        sync_block = {
            "time": {
                "field": source_cfg.time_field,
                "delay": "60s",
            }
        }

        # Frequency: use explicit value if set, otherwise default to max(bucket, 1m)
        if group_by_cfg.frequency:
            frequency = group_by_cfg.frequency
        else:
            frequency = group_by_cfg.time_bucket
            bucket_seconds = _parse_time_bucket_seconds(group_by_cfg.time_bucket)
            if bucket_seconds < 60:
                frequency = "1m"

        return {
            "source": source_block,
            "dest": {"index": f"{INDEX_PREFIX}{rule.id}"},
            "pivot": {
                "group_by": group_by,
                "aggregations": aggregations,
            },
            "frequency": frequency,
            "sync": sync_block,
        }

    def _cleanup_partial(self, transform_id: str, index_name: str) -> None:
        """Best-effort cleanup of partially provisioned resources."""
        try:
            es.transform.stop_transform(
                transform_id=transform_id,
                wait_for_completion=True,
                timeout="10s",
            )
        except Exception:
            pass
        try:
            es.transform.delete_transform(transform_id=transform_id)
        except Exception:
            pass
        try:
            es.indices.delete(index=index_name)
        except Exception:
            pass


# Module-level singleton
backend = ElasticMetricsBackend()

"""Tests for elastic_backend.py — transform body construction and field naming.

Bug 5 prevention: Ensure no ES reserved field names in output mappings or aggregations.
Bug 7 prevention: Zero-match transforms must return valid status.
"""

import pytest
from unittest.mock import MagicMock

ES_RESERVED_FIELDS = {"doc_count", "_source", "_id", "_type", "_index", "_score", "_routing"}


# ── Reserved field name tests (Bug 5 prevention) ────────────────────


class TestBuildAggregationsNoReservedNames:
    """Verify _build_aggregations never produces an ES reserved field name."""

    def test_count_uses_event_count_not_doc_count(self):
        from elastic_backend import _build_aggregations
        from models import ComputeConfig, ComputeType
        compute = ComputeConfig(type=ComputeType.count)
        aggs = _build_aggregations(compute, "timestamp")
        assert "event_count" in aggs
        assert "doc_count" not in aggs

    def test_sum_field_name(self):
        from elastic_backend import _build_aggregations
        from models import ComputeConfig, ComputeType
        compute = ComputeConfig(type=ComputeType.sum, field="response_time")
        aggs = _build_aggregations(compute, "timestamp")
        assert "sum_response_time" in aggs
        for key in aggs:
            assert key not in ES_RESERVED_FIELDS, f"Reserved name '{key}' in aggs"

    def test_avg_field_name(self):
        from elastic_backend import _build_aggregations
        from models import ComputeConfig, ComputeType
        compute = ComputeConfig(type=ComputeType.avg, field="latency")
        aggs = _build_aggregations(compute, "timestamp")
        assert "avg_latency" in aggs
        for key in aggs:
            assert key not in ES_RESERVED_FIELDS

    def test_distribution_field_name(self):
        from elastic_backend import _build_aggregations
        from models import ComputeConfig, ComputeType
        compute = ComputeConfig(type=ComputeType.distribution, field="latency")
        aggs = _build_aggregations(compute, "timestamp")
        assert "pct_latency" in aggs


class TestBuildTransformBody:
    """Verify the complete transform body structure."""

    def test_basic_count_transform(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule()
        body = backend._build_transform_body(rule)

        assert body["source"]["index"] == ["app-logs*"]
        assert "timestamp" in body["pivot"]["group_by"]
        assert "event_count" in body["pivot"]["aggregations"]
        assert body["dest"]["index"] == "l2m-metrics-rule-1"
        assert body["sync"]["time"]["field"] == "timestamp"

    def test_transform_with_filter_query(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule(
            source={"index_pattern": "app-logs*", "time_field": "timestamp",
                    "filter_query": {"term": {"level": "error"}}}
        )
        body = backend._build_transform_body(rule)
        assert body["source"]["query"] == {"term": {"level": "error"}}

    def test_transform_without_filter_uses_match_all(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule()
        body = backend._build_transform_body(rule)
        assert body["source"]["query"] == {"match_all": {}}

    def test_transform_includes_dimensions_in_group_by(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule(
            group_by={"time_bucket": "1m", "dimensions": ["service", "endpoint"], "frequency": None}
        )
        body = backend._build_transform_body(rule)
        gb = body["pivot"]["group_by"]
        assert "service" in gb
        assert gb["service"] == {"terms": {"field": "service"}}
        assert "endpoint" in gb

    def test_frequency_defaults_to_bucket_when_above_1m(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule(
            group_by={"time_bucket": "5m", "dimensions": [], "frequency": None}
        )
        body = backend._build_transform_body(rule)
        assert body["frequency"] == "5m"

    def test_frequency_floor_1m_for_small_buckets(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule(
            group_by={"time_bucket": "10s", "dimensions": [], "frequency": None}
        )
        body = backend._build_transform_body(rule)
        assert body["frequency"] == "1m"

    def test_explicit_frequency_overrides_default(self, make_log_metric_rule):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule(
            group_by={"time_bucket": "1m", "dimensions": [], "frequency": "15m"}
        )
        body = backend._build_transform_body(rule)
        assert body["frequency"] == "15m"

    def test_no_reserved_fields_in_transform_aggs(self, make_log_metric_rule):
        """Comprehensive: no reserved field name in any aggregation keys."""
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule()
        body = backend._build_transform_body(rule)
        agg_keys = set(body["pivot"]["aggregations"].keys())
        for key in agg_keys:
            assert key not in ES_RESERVED_FIELDS, f"Reserved field '{key}' in aggregations"


class TestCreateMetricsIndex:
    """Verify _create_metrics_index produces correct mapping properties."""

    def test_count_mapping_uses_event_count(self, make_log_metric_rule, mock_es_client):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule()
        backend._create_metrics_index(rule, "test-policy")

        call_args = mock_es_client.indices.create.call_args
        body = call_args.kwargs.get("body", call_args[1].get("body") if len(call_args) > 1 else None)
        props = body["mappings"]["properties"]
        assert "event_count" in props
        assert "doc_count" not in props

    def test_sum_mapping_field_name(self, make_log_metric_rule, mock_es_client):
        from elastic_backend import ElasticMetricsBackend
        backend = ElasticMetricsBackend()
        rule = make_log_metric_rule(
            compute={"type": "sum", "field": "response_time", "percentiles": None}
        )
        backend._create_metrics_index(rule, "test-policy")

        call_args = mock_es_client.indices.create.call_args
        body = call_args.kwargs.get("body", call_args[1].get("body") if len(call_args) > 1 else None)
        props = body["mappings"]["properties"]
        assert "sum_response_time" in props
        assert props["sum_response_time"]["type"] == "double"


class TestMapTransformState:
    def test_known_states(self):
        from elastic_backend import _map_transform_state
        from backend import TransformHealth
        assert _map_transform_state("started") == TransformHealth.green
        assert _map_transform_state("indexing") == TransformHealth.green
        assert _map_transform_state("stopping") == TransformHealth.yellow
        assert _map_transform_state("stopped") == TransformHealth.stopped
        assert _map_transform_state("aborting") == TransformHealth.red
        assert _map_transform_state("failed") == TransformHealth.red

    def test_unknown_state(self):
        from elastic_backend import _map_transform_state
        from backend import TransformHealth
        assert _map_transform_state("something_new") == TransformHealth.unknown


class TestGetStatus:
    def test_not_found_returns_unknown(self, mock_es_client):
        from elastic_backend import ElasticMetricsBackend
        from elasticsearch import NotFoundError
        from backend import TransformHealth
        mock_es_client.transform.get_transform_stats.side_effect = NotFoundError(
            404, "not found", {}
        )
        backend = ElasticMetricsBackend()
        status = backend.get_status(999)
        assert status.health == TransformHealth.unknown
        assert "not found" in (status.error or "").lower()

    def test_zero_docs_processed_returns_valid_status(self, mock_es_client):
        """Bug 7 prevention: zero-match transforms must return valid status."""
        from elastic_backend import ElasticMetricsBackend
        from backend import TransformHealth
        mock_es_client.transform.get_transform_stats.return_value = {
            "transforms": [{
                "state": "started",
                "stats": {"documents_processed": 0, "documents_indexed": 0},
                "checkpointing": {"last": {}},
            }]
        }
        backend = ElasticMetricsBackend()
        status = backend.get_status(1)
        assert status.health == TransformHealth.green
        assert status.docs_processed == 0
        assert status.docs_indexed == 0


class TestParseTimeBucketSeconds:
    def test_seconds(self):
        from elastic_backend import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("10s") == 10

    def test_minutes(self):
        from elastic_backend import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("5m") == 300

    def test_hours(self):
        from elastic_backend import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("1h") == 3600

    def test_days(self):
        from elastic_backend import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("1d") == 86400

    def test_empty_returns_60(self):
        from elastic_backend import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("") == 60

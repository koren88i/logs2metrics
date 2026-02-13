"""Tests for cost_estimator.py.

All tests use mock_es_connector to avoid real ES calls.
"""

import pytest


class TestEstimateCost:
    def test_zero_docs_returns_zero_everything(self, mock_es_connector, make_rule_create):
        from connector_models import IndexStats
        from cost_estimator import estimate_cost

        mock_es_connector["get_index_stats"].return_value = IndexStats(
            index="app-logs", doc_count=0, store_size_bytes=0,
            store_size_human="0b", query_total=0, query_time_ms=0,
        )
        rule = make_rule_create()
        result = estimate_cost(rule)
        assert result.log_storage_gb == 0
        assert result.metric_storage_gb == 0
        assert result.savings_gb == 0
        assert result.estimated_series_count == 0

    def test_basic_count_rule_math(self, mock_es_connector, make_rule_create):
        from cost_estimator import estimate_cost

        rule = make_rule_create()
        result = estimate_cost(rule)
        # 1 dimension with cardinality=10 => series=10
        # 1m bucket => 86400/60 = 1440 points/day/series => 14400 total
        assert result.estimated_series_count == 10
        assert result.metric_points_per_day == 14400
        assert result.docs_per_day == 100_000
        assert result.savings_gb > 0

    def test_no_dimensions_gives_series_count_1(self, mock_es_connector, make_rule_create):
        from cost_estimator import estimate_cost
        from models import GroupByConfig

        rule = make_rule_create(group_by=GroupByConfig(time_bucket="1m", dimensions=[]))
        result = estimate_cost(rule)
        assert result.estimated_series_count == 1

    def test_multiple_dimensions_multiply_cardinalities(self, mock_es_connector, make_rule_create):
        from cost_estimator import estimate_cost
        from connector_models import FieldCardinality
        from models import GroupByConfig

        mock_es_connector["get_field_cardinality"].return_value = FieldCardinality(
            index="app-logs", field="x", cardinality=10,
        )
        rule = make_rule_create(
            group_by=GroupByConfig(time_bucket="1m", dimensions=["service", "endpoint"])
        )
        result = estimate_cost(rule)
        assert result.estimated_series_count == 100  # 10 * 10

    def test_cardinality_fetch_failure_uses_100(self, mock_es_connector, make_rule_create):
        from cost_estimator import estimate_cost

        mock_es_connector["get_field_cardinality"].side_effect = Exception("ES down")
        rule = make_rule_create()
        result = estimate_cost(rule)
        assert result.estimated_series_count == 100  # fallback

    def test_larger_bucket_fewer_points(self, mock_es_connector, make_rule_create):
        from cost_estimator import estimate_cost
        from models import GroupByConfig

        rule_1m = make_rule_create(group_by=GroupByConfig(time_bucket="1m", dimensions=[]))
        rule_5m = make_rule_create(group_by=GroupByConfig(time_bucket="5m", dimensions=[]))
        r1 = estimate_cost(rule_1m)
        r5 = estimate_cost(rule_5m)
        assert r5.metric_points_per_day < r1.metric_points_per_day

    def test_index_pattern_strip_star(self, mock_es_connector, make_rule_create):
        from cost_estimator import estimate_cost

        rule = make_rule_create()
        estimate_cost(rule)
        mock_es_connector["get_index_stats"].assert_called_with("app-logs")


class TestParseTimeBucketSeconds:
    def test_seconds(self):
        from cost_estimator import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("10s") == 10

    def test_minutes(self):
        from cost_estimator import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("5m") == 300

    def test_hours(self):
        from cost_estimator import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("1h") == 3600

    def test_empty_returns_60(self):
        from cost_estimator import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("") == 60

    def test_invalid_returns_60(self):
        from cost_estimator import _parse_time_bucket_seconds
        assert _parse_time_bucket_seconds("abc") == 60

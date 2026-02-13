"""Tests for Pydantic model validation in models.py and connector_models.py."""

import pytest
from pydantic import ValidationError


class TestSourceConfig:
    def test_valid_source(self):
        from models import SourceConfig
        sc = SourceConfig(index_pattern="app-logs*")
        assert sc.index_pattern == "app-logs*"
        assert sc.time_field == "timestamp"
        assert sc.filter_query is None

    def test_empty_index_pattern_rejected(self):
        from models import SourceConfig
        with pytest.raises(ValidationError):
            SourceConfig(index_pattern="")

    def test_custom_time_field(self):
        from models import SourceConfig
        sc = SourceConfig(index_pattern="logs*", time_field="@timestamp")
        assert sc.time_field == "@timestamp"

    def test_filter_query_accepts_dict(self):
        from models import SourceConfig
        sc = SourceConfig(index_pattern="x*", filter_query={"term": {"level": "error"}})
        assert sc.filter_query == {"term": {"level": "error"}}


class TestGroupByConfig:
    def test_defaults(self):
        from models import GroupByConfig
        g = GroupByConfig()
        assert g.time_bucket == "1m"
        assert g.dimensions == []
        assert g.frequency is None

    def test_max_dimensions_length(self):
        from models import GroupByConfig
        g = GroupByConfig(dimensions=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
        assert len(g.dimensions) == 10

    def test_exceeding_max_dimensions_rejected(self):
        from models import GroupByConfig
        with pytest.raises(ValidationError):
            GroupByConfig(dimensions=[f"d{i}" for i in range(11)])

    def test_custom_frequency(self):
        from models import GroupByConfig
        g = GroupByConfig(frequency="5m")
        assert g.frequency == "5m"

    def test_default_sync_delay_is_30s(self):
        from models import GroupByConfig
        g = GroupByConfig()
        assert g.sync_delay == "30s"

    def test_custom_sync_delay(self):
        from models import GroupByConfig
        g = GroupByConfig(sync_delay="5m")
        assert g.sync_delay == "5m"

    def test_sync_delay_backward_compatible(self):
        """Existing rules without sync_delay in JSON get the default."""
        from models import GroupByConfig
        g = GroupByConfig(**{"time_bucket": "1m", "dimensions": []})
        assert g.sync_delay == "30s"


class TestComputeConfig:
    def test_count_type_no_field(self):
        from models import ComputeConfig, ComputeType
        c = ComputeConfig(type=ComputeType.count)
        assert c.field is None

    def test_sum_type_with_field(self):
        from models import ComputeConfig, ComputeType
        c = ComputeConfig(type=ComputeType.sum, field="response_time")
        assert c.field == "response_time"

    def test_distribution_with_percentiles(self):
        from models import ComputeConfig, ComputeType
        c = ComputeConfig(
            type=ComputeType.distribution, field="latency", percentiles=[50, 90, 99]
        )
        assert c.percentiles == [50, 90, 99]

    def test_invalid_compute_type_rejected(self):
        from models import ComputeConfig
        with pytest.raises(ValidationError):
            ComputeConfig(type="invalid")


class TestBackendConfig:
    def test_defaults(self):
        from models import BackendConfig, BackendType
        b = BackendConfig()
        assert b.type == BackendType.elastic
        assert b.retention_days == 450

    def test_retention_too_low(self):
        from models import BackendConfig
        with pytest.raises(ValidationError):
            BackendConfig(retention_days=0)

    def test_retention_too_high(self):
        from models import BackendConfig
        with pytest.raises(ValidationError):
            BackendConfig(retention_days=731)


class TestRuleCreate:
    def test_name_min_length(self):
        from models import RuleCreate, SourceConfig, ComputeConfig, ComputeType
        with pytest.raises(ValidationError):
            RuleCreate(
                name="",
                source=SourceConfig(index_pattern="x*"),
                compute=ComputeConfig(type=ComputeType.count),
            )

    def test_name_max_length(self):
        from models import RuleCreate, SourceConfig, ComputeConfig, ComputeType
        with pytest.raises(ValidationError):
            RuleCreate(
                name="x" * 201,
                source=SourceConfig(index_pattern="x*"),
                compute=ComputeConfig(type=ComputeType.count),
            )

    def test_valid_minimal_rule(self, make_rule_create):
        rule = make_rule_create()
        assert rule.name == "test-rule"
        assert rule.status.value == "draft"


class TestRuleResponse:
    def test_from_db_round_trip(self, make_log_metric_rule):
        from models import RuleResponse
        rule = make_log_metric_rule()
        resp = RuleResponse.from_db(rule)
        assert resp.id == 1
        assert resp.name == "test-rule"
        assert resp.source.index_pattern == "app-logs*"
        assert resp.compute.type.value == "count"
        assert resp.status.value == "draft"

    def test_from_db_with_origin(self, make_log_metric_rule):
        from models import RuleResponse
        rule = make_log_metric_rule(origin={
            "dashboard_id": "abc", "dashboard_title": "D",
            "panel_id": "p1", "panel_title": "P",
        })
        resp = RuleResponse.from_db(rule)
        assert resp.origin is not None
        assert resp.origin.dashboard_id == "abc"

    def test_from_db_without_origin(self, make_log_metric_rule):
        from models import RuleResponse
        rule = make_log_metric_rule(origin={})
        resp = RuleResponse.from_db(rule)
        assert resp.origin is None

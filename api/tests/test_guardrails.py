"""Tests for the 4 guardrail checks in guardrails.py."""

import pytest


class TestDimensionLimit:
    def test_within_limit_passes(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        from models import GroupByConfig
        rule = make_rule_create(group_by=GroupByConfig(dimensions=["a", "b", "c"]))
        report = evaluate(rule)
        dim_check = next(r for r in report.results if r.name == "dimension_limit")
        assert dim_check.passed is True

    def test_exceeds_limit_fails(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate, MAX_DIMENSIONS
        from models import GroupByConfig
        dims = [f"d{i}" for i in range(MAX_DIMENSIONS + 1)]
        rule = make_rule_create(group_by=GroupByConfig(dimensions=dims))
        report = evaluate(rule)
        dim_check = next(r for r in report.results if r.name == "dimension_limit")
        assert dim_check.passed is False
        assert dim_check.suggested_fix is not None

    def test_exactly_at_limit_passes(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate, MAX_DIMENSIONS
        from models import GroupByConfig
        dims = [f"d{i}" for i in range(MAX_DIMENSIONS)]
        rule = make_rule_create(group_by=GroupByConfig(dimensions=dims))
        report = evaluate(rule)
        dim_check = next(r for r in report.results if r.name == "dimension_limit")
        assert dim_check.passed is True


class TestCardinality:
    def test_low_cardinality_passes(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        rule = make_rule_create()
        report = evaluate(rule)
        card_check = next(r for r in report.results if r.name == "cardinality")
        assert card_check.passed is True

    def test_high_cardinality_fails(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        from connector_models import FieldCardinality
        mock_es_connector["get_field_cardinality"].return_value = FieldCardinality(
            index="x", field="x", cardinality=200_000,
        )
        rule = make_rule_create()
        report = evaluate(rule)
        card_check = next(r for r in report.results if r.name == "cardinality")
        assert card_check.passed is False


class TestHighCardinalityFields:
    def test_known_high_cardinality_field_fails(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        from models import GroupByConfig
        rule = make_rule_create(
            group_by=GroupByConfig(dimensions=["user_id", "service"])
        )
        report = evaluate(rule)
        hcf = next(r for r in report.results if r.name == "high_cardinality_fields")
        assert hcf.passed is False
        assert "user_id" in hcf.explanation

    def test_normal_fields_pass(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        rule = make_rule_create()
        report = evaluate(rule)
        hcf = next(r for r in report.results if r.name == "high_cardinality_fields")
        assert hcf.passed is True


class TestNetSavings:
    def test_savings_positive_passes(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        rule = make_rule_create()
        report = evaluate(rule)
        ns = next(r for r in report.results if r.name == "net_savings")
        assert ns.passed is True

    def test_metric_exceeds_log_fails(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        from connector_models import IndexStats, FieldCardinality
        mock_es_connector["get_index_stats"].return_value = IndexStats(
            index="x", doc_count=100, store_size_bytes=1000,
            store_size_human="1kb", query_total=0, query_time_ms=0,
        )
        mock_es_connector["get_field_cardinality"].return_value = FieldCardinality(
            index="x", field="x", cardinality=50_000,
        )
        rule = make_rule_create()
        report = evaluate(rule)
        ns = next(r for r in report.results if r.name == "net_savings")
        assert ns.passed is False


class TestAllPassed:
    def test_all_pass_when_valid(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate
        rule = make_rule_create()
        report = evaluate(rule)
        assert report.all_passed is True

    def test_not_all_pass_when_any_fails(self, mock_es_connector, make_rule_create):
        from guardrails import evaluate, MAX_DIMENSIONS
        from models import GroupByConfig
        rule = make_rule_create(
            group_by=GroupByConfig(dimensions=[f"d{i}" for i in range(MAX_DIMENSIONS + 1)])
        )
        report = evaluate(rule)
        assert report.all_passed is False

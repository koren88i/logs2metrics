"""Tests for prometheus_exporter.py — metric collection and Prometheus exposition."""

import pytest
from unittest.mock import MagicMock, patch
from elasticsearch import NotFoundError


# ── sanitize_metric_name ─────────────────────────────────────────────


class TestSanitizeMetricName:

    def test_hyphens_to_underscores(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("error-rate") == "error_rate"

    def test_dots_to_underscores(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("service.latency") == "service_latency"

    def test_spaces_and_special_chars(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("API Error Rate!") == "api_error_rate"

    def test_already_valid(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("simple_name") == "simple_name"

    def test_collapses_multiple_underscores(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("a--b__c") == "a_b_c"

    def test_strips_leading_trailing_underscores(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("-leading-") == "leading"

    def test_preserves_digits(self):
        from prometheus_exporter import sanitize_metric_name
        assert sanitize_metric_name("errors_5xx") == "errors_5xx"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_prom_es():
    """Patch the ES client used by prometheus_exporter."""
    mock = MagicMock()
    with patch("prometheus_exporter.Elasticsearch", return_value=mock):
        yield mock


@pytest.fixture
def mock_prom_backend():
    """Patch the metrics_backend used by prometheus_exporter."""
    mock = MagicMock()
    with patch("prometheus_exporter.metrics_backend", mock):
        yield mock


@pytest.fixture(autouse=True)
def clean_registry():
    """Clear the prometheus registry between tests."""
    from prometheus_exporter import _clear_rule_gauges
    _clear_rule_gauges()
    yield
    _clear_rule_gauges()


def _make_es_response(docs):
    """Helper: wrap docs in ES search response structure."""
    return {
        "hits": {
            "hits": [{"_source": doc} for doc in docs],
        }
    }


# ── _collect_rule_metrics tests ───────────────────────────────────────


class TestCollectRuleMetricsCount:

    def test_count_metric_created(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "service": "auth", "event_count": 42},
        ])
        rule = make_log_metric_rule(
            name="error-count", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_error_count_event_count" in _rule_gauges
        sample = _rule_gauges["l2m_rule_error_count_event_count"].labels(
            rule_name="error-count", service="auth",
        )
        assert sample._value.get() == 42

    def test_count_no_dimensions(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "event_count": 100},
        ])
        rule = make_log_metric_rule(
            name="total-events", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": []},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_total_events_event_count" in _rule_gauges
        # rule_name is always present even with no dimensions
        sample = _rule_gauges["l2m_rule_total_events_event_count"].labels(
            rule_name="total-events",
        )
        assert sample._value.get() == 100

    def test_count_multiple_dimension_values(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:01:00Z", "service": "auth", "event_count": 10},
            {"timestamp": "2024-01-01T00:01:00Z", "service": "api", "event_count": 20},
        ])
        rule = make_log_metric_rule(
            name="errors", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        gauge = _rule_gauges["l2m_rule_errors_event_count"]
        assert gauge.labels(rule_name="errors", service="auth")._value.get() == 10
        assert gauge.labels(rule_name="errors", service="api")._value.get() == 20


class TestCollectRuleMetricsSum:

    def test_sum_metric_name_includes_field(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "service": "api", "sum_bytes": 1024},
        ])
        rule = make_log_metric_rule(
            name="total-bytes", status="active",
            compute={"type": "sum", "field": "bytes"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_total_bytes_sum_bytes" in _rule_gauges
        assert _rule_gauges["l2m_rule_total_bytes_sum_bytes"].labels(
            rule_name="total-bytes", service="api",
        )._value.get() == 1024


class TestCollectRuleMetricsAvg:

    def test_avg_metric_name_includes_field(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "service": "api", "avg_response_time_ms": 234.5},
        ])
        rule = make_log_metric_rule(
            name="api-latency", status="active",
            compute={"type": "avg", "field": "response_time_ms"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_api_latency_avg_response_time_ms" in _rule_gauges
        assert _rule_gauges["l2m_rule_api_latency_avg_response_time_ms"].labels(
            rule_name="api-latency", service="api",
        )._value.get() == 234.5


class TestCollectRuleMetricsDistribution:

    def test_distribution_creates_per_percentile_metrics(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "service": "api",
                "pct_response_time_ms": {
                    "values": {"50.0": 120.5, "95.0": 780.0, "99.0": 1200.0}
                },
            },
        ])
        rule = make_log_metric_rule(
            name="latency-dist", status="active",
            compute={"type": "distribution", "field": "response_time_ms", "percentiles": [50, 95, 99]},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_latency_dist_p50_response_time_ms" in _rule_gauges
        assert "l2m_rule_latency_dist_p95_response_time_ms" in _rule_gauges
        assert "l2m_rule_latency_dist_p99_response_time_ms" in _rule_gauges
        assert _rule_gauges["l2m_rule_latency_dist_p50_response_time_ms"].labels(
            rule_name="latency-dist", service="api",
        )._value.get() == 120.5
        assert _rule_gauges["l2m_rule_latency_dist_p95_response_time_ms"].labels(
            rule_name="latency-dist", service="api",
        )._value.get() == 780.0

    def test_distribution_without_values_wrapper(self, mock_prom_es, make_log_metric_rule):
        """Handle ES response where percentiles are flat (no 'values' key)."""
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "service": "api",
                "pct_latency": {"50.0": 100.0, "90.0": 200.0},
            },
        ])
        rule = make_log_metric_rule(
            name="flat-pct", status="active",
            compute={"type": "distribution", "field": "latency"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_flat_pct_p50_latency" in _rule_gauges
        assert "l2m_rule_flat_pct_p90_latency" in _rule_gauges

    def test_distribution_skips_null_values(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "service": "api",
                "pct_latency": {"values": {"50.0": 100.0, "99.0": None}},
            },
        ])
        rule = make_log_metric_rule(
            name="null-pct", status="active",
            compute={"type": "distribution", "field": "latency"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert "l2m_rule_null_pct_p50_latency" in _rule_gauges
        assert "l2m_rule_null_pct_p99_latency" not in _rule_gauges


# ── Edge cases ────────────────────────────────────────────────────────


class TestCollectRuleMetricsEdgeCases:

    def test_missing_index_does_not_raise(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.side_effect = NotFoundError(
            message="index_not_found",
            meta=MagicMock(),
            body={"error": "index_not_found_exception"},
        )
        rule = make_log_metric_rule(name="missing", status="active", compute={"type": "count"})

        _collect_rule_metrics(mock_prom_es, rule)

        assert len(_rule_gauges) == 0

    def test_empty_hits_produces_no_gauges(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([])
        rule = make_log_metric_rule(name="empty", status="active", compute={"type": "count"})

        _collect_rule_metrics(mock_prom_es, rule)

        assert len(_rule_gauges) == 0

    def test_deduplicates_by_dimension_combo(self, mock_prom_es, make_log_metric_rule):
        """When multiple docs have the same dimensions, keep only the first (latest)."""
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:02:00Z", "service": "api", "event_count": 99},
            {"timestamp": "2024-01-01T00:01:00Z", "service": "api", "event_count": 50},
        ])
        rule = make_log_metric_rule(
            name="dedup", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        # Should keep 99 (first/latest), not 50
        assert _rule_gauges["l2m_rule_dedup_event_count"].labels(
            rule_name="dedup", service="api",
        )._value.get() == 99

    def test_missing_dimension_defaults_to_unknown(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "event_count": 5},
        ])
        rule = make_log_metric_rule(
            name="missing-dim", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": ["service"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        assert _rule_gauges["l2m_rule_missing_dim_event_count"].labels(
            rule_name="missing-dim", service="unknown",
        )._value.get() == 5

    def test_multiple_dimensions(self, mock_prom_es, make_log_metric_rule):
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "service": "api", "endpoint": "/users", "event_count": 77},
        ])
        rule = make_log_metric_rule(
            name="multi-dim", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": ["service", "endpoint"]},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        gauge = _rule_gauges["l2m_rule_multi_dim_event_count"]
        assert gauge.labels(rule_name="multi-dim", service="api", endpoint="/users")._value.get() == 77

    def test_rule_name_label_always_present(self, mock_prom_es, make_log_metric_rule):
        """rule_name label is included even with no dimensions, enabling Grafana variable filtering."""
        from prometheus_exporter import _collect_rule_metrics, _rule_gauges

        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "event_count": 10},
        ])
        rule = make_log_metric_rule(
            name="no-dims", status="active",
            compute={"type": "count"},
            group_by={"time_bucket": "1m", "dimensions": []},
        )

        _collect_rule_metrics(mock_prom_es, rule)

        gauge = _rule_gauges["l2m_rule_no_dims_event_count"]
        assert gauge.labels(rule_name="no-dims")._value.get() == 10


# ── Transform health tests ───────────────────────────────────────────


class TestCollectTransformHealth:

    @pytest.mark.parametrize("health_enum,expected_val", [
        ("green", 1),
        ("yellow", 2),
        ("red", 3),
        ("stopped", 4),
        ("unknown", 0),
    ])
    def test_health_mapping(self, mock_prom_backend, make_log_metric_rule, health_enum, expected_val):
        from prometheus_exporter import _collect_transform_health, _transform_health
        from backend import TransformHealth

        mock_prom_backend.get_status.return_value = MagicMock(
            health=TransformHealth(health_enum),
            docs_processed=1000,
            docs_indexed=100,
        )
        rule = make_log_metric_rule(id=5, name="health-test", status="active")

        _collect_transform_health(rule)

        labels = {"rule_id": "5", "rule_name": "health-test"}
        assert _transform_health.labels(**labels)._value.get() == expected_val

    def test_docs_processed_and_indexed(self, mock_prom_backend, make_log_metric_rule):
        from prometheus_exporter import _collect_transform_health, _transform_docs_processed, _transform_docs_indexed
        from backend import TransformHealth

        mock_prom_backend.get_status.return_value = MagicMock(
            health=TransformHealth.green,
            docs_processed=5000,
            docs_indexed=500,
        )
        rule = make_log_metric_rule(id=3, name="docs-test", status="active")

        _collect_transform_health(rule)

        labels = {"rule_id": "3", "rule_name": "docs-test"}
        assert _transform_docs_processed.labels(**labels)._value.get() == 5000
        assert _transform_docs_indexed.labels(**labels)._value.get() == 500


# ── collect_and_generate integration ──────────────────────────────────


class TestCollectAndGenerate:

    def test_returns_bytes(self, mock_prom_es, mock_prom_backend):
        from prometheus_exporter import collect_and_generate

        # No active rules in DB — should still return valid output
        with patch("prometheus_exporter.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session
            session.exec.return_value.all.return_value = []

            result = collect_and_generate()

        assert isinstance(result, bytes)

    def test_contains_health_metric_names(self, mock_prom_es, mock_prom_backend, make_log_metric_rule):
        from prometheus_exporter import collect_and_generate
        from backend import TransformHealth

        rule = make_log_metric_rule(id=1, name="test-rule", status="active", compute={"type": "count"})
        mock_prom_es.search.return_value = _make_es_response([
            {"timestamp": "2024-01-01T00:00:00Z", "service": "api", "event_count": 42},
        ])
        mock_prom_backend.get_status.return_value = MagicMock(
            health=TransformHealth.green, docs_processed=100, docs_indexed=10,
        )

        with patch("prometheus_exporter.Session") as mock_session_cls:
            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            mock_session_cls.return_value = session
            session.exec.return_value.all.return_value = [rule]

            result = collect_and_generate()

        text = result.decode()
        assert "l2m_rule_test_rule_event_count" in text
        assert 'rule_name="test-rule"' in text
        assert "l2m_transform_health" in text
        assert "l2m_transform_docs_processed" in text

"""Tests for the scoring engine (scoring.py).

Tests all 6 signals: date_histogram, numeric_aggs, no_raw_docs,
aggregatable_dimensions, lookback_window, auto_refresh.
Max possible score = 95.
"""

import pytest


def _get_signal(result, name):
    return next(b for b in result.breakdown if b.signal == name)


class TestScoreDateHistogram:
    def test_has_date_histogram_gives_25(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(agg_types=["date_histogram", "count"])
        result = score_panel(panel)
        assert _get_signal(result, "date_histogram").points == 25

    def test_no_date_histogram_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(agg_types=["terms", "count"])
        result = score_panel(panel)
        assert _get_signal(result, "date_histogram").points == 0


class TestScoreNumericAggs:
    def test_all_numeric_gives_20(self, make_panel_analysis):
        from scoring import score_panel
        from connector_models import MetricInfo
        panel = make_panel_analysis(
            metrics=[MetricInfo(type="count"), MetricInfo(type="sum", field="x")],
        )
        result = score_panel(panel)
        assert _get_signal(result, "numeric_aggs").points == 20

    def test_non_numeric_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        from connector_models import MetricInfo
        panel = make_panel_analysis(metrics=[MetricInfo(type="top_hits")])
        result = score_panel(panel)
        assert _get_signal(result, "numeric_aggs").points == 0

    def test_no_metrics_no_raw_docs_gives_10(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(metrics=[], has_raw_docs=False)
        result = score_panel(panel)
        assert _get_signal(result, "numeric_aggs").points == 10

    def test_raw_docs_no_metrics_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(metrics=[], has_raw_docs=True)
        result = score_panel(panel)
        assert _get_signal(result, "numeric_aggs").points == 0


class TestScoreNoRawDocs:
    def test_no_raw_docs_gives_15(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(has_raw_docs=False)
        result = score_panel(panel)
        assert _get_signal(result, "no_raw_docs").points == 15

    def test_has_raw_docs_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(has_raw_docs=True)
        result = score_panel(panel)
        assert _get_signal(result, "no_raw_docs").points == 0


class TestScoreAggregatableDimensions:
    def test_all_aggregatable_gives_10(self, make_panel_analysis, make_field_mapping):
        from scoring import score_panel
        panel = make_panel_analysis(group_by_fields=["service", "endpoint"])
        ft = make_field_mapping({"service": "keyword", "endpoint": "keyword"})
        result = score_panel(panel, field_types=ft)
        assert _get_signal(result, "aggregatable_dimensions").points == 10

    def test_non_aggregatable_gives_0(self, make_panel_analysis, make_field_mapping):
        from scoring import score_panel
        panel = make_panel_analysis(group_by_fields=["message"])
        ft = make_field_mapping({"message": "text"})
        result = score_panel(panel, field_types=ft)
        assert _get_signal(result, "aggregatable_dimensions").points == 0

    def test_fields_present_but_no_types_gives_5(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(group_by_fields=["service"])
        result = score_panel(panel, field_types=None)
        assert _get_signal(result, "aggregatable_dimensions").points == 5

    def test_no_group_by_gives_5(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(group_by_fields=[])
        result = score_panel(panel)
        assert _get_signal(result, "aggregatable_dimensions").points == 5


class TestScoreLookback:
    def test_7d_gives_15(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from="now-7d")
        assert _get_signal(result, "lookback_window").points == 15

    def test_30d_gives_15(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from="now-30d")
        assert _get_signal(result, "lookback_window").points == 15

    def test_1d_gives_5(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from="now-1d")
        assert _get_signal(result, "lookback_window").points == 5

    def test_no_lookback_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from=None)
        assert _get_signal(result, "lookback_window").points == 0

    def test_unparseable_lookback_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from="2024-01-01")
        assert _get_signal(result, "lookback_window").points == 0

    def test_weeks_parsed(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from="now-2w")
        assert _get_signal(result, "lookback_window").points == 15

    def test_months_parsed(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), dashboard_time_from="now-1M")
        assert _get_signal(result, "lookback_window").points == 15


class TestScoreAutoRefresh:
    def test_enabled_gives_10(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), refresh_interval_ms=30000)
        assert _get_signal(result, "auto_refresh").points == 10

    def test_disabled_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), refresh_interval_ms=None)
        assert _get_signal(result, "auto_refresh").points == 0

    def test_zero_gives_0(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis(), refresh_interval_ms=0)
        assert _get_signal(result, "auto_refresh").points == 0


class TestMaxScore:
    def test_perfect_panel_scores_95(self, make_panel_analysis, make_field_mapping):
        from scoring import score_panel
        from connector_models import MetricInfo
        panel = make_panel_analysis(
            agg_types=["date_histogram", "count"],
            metrics=[MetricInfo(type="count")],
            group_by_fields=["service"],
            has_raw_docs=False,
        )
        ft = make_field_mapping({"service": "keyword"})
        result = score_panel(
            panel, field_types=ft,
            dashboard_time_from="now-30d", refresh_interval_ms=30000,
        )
        assert result.total == 95
        assert result.max_total == 95

    def test_max_total_always_95(self, make_panel_analysis):
        from scoring import score_panel
        result = score_panel(make_panel_analysis())
        assert result.max_total == 95


class TestRecommendation:
    def test_raw_docs_recommendation(self, make_panel_analysis):
        from scoring import score_panel
        panel = make_panel_analysis(has_raw_docs=True)
        result = score_panel(panel)
        assert "raw log lines" in result.recommendation_text

    def test_strong_candidate(self, make_panel_analysis, make_field_mapping):
        from scoring import score_panel
        from connector_models import MetricInfo
        panel = make_panel_analysis(
            agg_types=["date_histogram", "count"],
            metrics=[MetricInfo(type="count")],
            group_by_fields=["service"],
            has_raw_docs=False,
        )
        ft = make_field_mapping({"service": "keyword"})
        result = score_panel(
            panel, field_types=ft,
            dashboard_time_from="now-30d", refresh_interval_ms=30000,
        )
        assert "Strong candidate" in result.recommendation_text

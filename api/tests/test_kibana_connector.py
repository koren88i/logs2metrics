"""Tests for kibana_connector.py — NDJSON import, vis cloning, panel resolution.

Bug 4 prevention: data view must be included in NDJSON batch.
"""

import json
import pytest
from unittest.mock import MagicMock, patch


class TestCloneAndRewireVisualization:
    """Test _clone_and_rewire_visualization (pure function, no HTTP)."""

    def _make_original_vis(self):
        return {
            "attributes": {
                "visState": json.dumps({
                    "type": "line",
                    "aggs": [
                        {"type": "count", "schema": "metric", "params": {}},
                        {"type": "date_histogram", "schema": "segment",
                         "params": {"field": "timestamp"}},
                        {"type": "terms", "schema": "group",
                         "params": {"field": "service"}},
                    ],
                }),
                "title": "Original",
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": json.dumps({
                        "query": {"query": "level:error", "language": "kuery"},
                        "filter": [{"match": {"level": "error"}}],
                    })
                },
            },
        }

    def test_metric_agg_rewired_for_count(self):
        from kibana_connector import _clone_and_rewire_visualization
        vis = self._make_original_vis()
        result = _clone_and_rewire_visualization(
            vis, "new-vis-id", "new-dv-id", "My Metric", "count", None
        )
        vis_state = json.loads(result["attributes"]["visState"])
        metric_agg = next(a for a in vis_state["aggs"] if a["schema"] == "metric")
        assert metric_agg["type"] == "sum"
        assert metric_agg["params"]["field"] == "event_count"

    def test_metric_agg_rewired_for_sum(self):
        from kibana_connector import _clone_and_rewire_visualization
        vis = self._make_original_vis()
        result = _clone_and_rewire_visualization(
            vis, "new-vis-id", "new-dv-id", "My Metric", "sum", "response_time"
        )
        vis_state = json.loads(result["attributes"]["visState"])
        metric_agg = next(a for a in vis_state["aggs"] if a["schema"] == "metric")
        assert metric_agg["type"] == "sum"
        assert metric_agg["params"]["field"] == "sum_response_time"

    def test_filter_cleared_in_clone(self):
        from kibana_connector import _clone_and_rewire_visualization
        vis = self._make_original_vis()
        result = _clone_and_rewire_visualization(
            vis, "new-vis-id", "new-dv-id", "My Metric", "count", None
        )
        ss = json.loads(result["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
        assert ss["query"]["query"] == ""
        assert ss["filter"] == []

    def test_reference_points_to_new_data_view(self):
        from kibana_connector import _clone_and_rewire_visualization
        vis = self._make_original_vis()
        result = _clone_and_rewire_visualization(
            vis, "new-vis-id", "new-dv-id", "My Metric", "count", None
        )
        assert result["id"] == "new-vis-id"
        assert result["references"][0]["id"] == "new-dv-id"
        assert result["references"][0]["type"] == "index-pattern"

    def test_title_updated(self):
        from kibana_connector import _clone_and_rewire_visualization
        vis = self._make_original_vis()
        result = _clone_and_rewire_visualization(
            vis, "v", "dv", "New Title", "count", None
        )
        assert result["attributes"]["title"] == "New Title"

    def test_non_metric_aggs_preserved(self):
        from kibana_connector import _clone_and_rewire_visualization
        vis = self._make_original_vis()
        result = _clone_and_rewire_visualization(
            vis, "v", "dv", "T", "count", None
        )
        vis_state = json.loads(result["attributes"]["visState"])
        segment_agg = next(a for a in vis_state["aggs"] if a["schema"] == "segment")
        assert segment_agg["type"] == "date_histogram"
        group_agg = next(a for a in vis_state["aggs"] if a["schema"] == "group")
        assert group_agg["type"] == "terms"


class TestDateHistogramIntervalExtraction:
    """Verify _parse_visualization extracts date_histogram interval."""

    def _make_vis_response(self, interval_key=None, interval_value=None):
        """Build a mock Kibana saved-object response for a visualization."""
        params = {"field": "timestamp"}
        if interval_key:
            params[interval_key] = interval_value
        aggs = [
            {"type": "count", "schema": "metric", "params": {}},
            {"type": "date_histogram", "schema": "segment", "params": params},
        ]
        return {
            "attributes": {
                "visState": json.dumps({"type": "line", "aggs": aggs}),
                "title": "Test",
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": json.dumps({
                        "query": {"query": "", "language": "kuery"}, "filter": [],
                    })
                },
            },
            "references": [],
        }

    def _parse(self, vis_response):
        from kibana_connector import _parse_visualization
        mock_response = MagicMock()
        mock_response.json.return_value = vis_response
        mock_response.raise_for_status.return_value = None
        with patch("kibana_connector._get_client_and_url") as mock_get:
            mock_client = MagicMock()
            mock_client.get.return_value = mock_response
            mock_get.return_value = (mock_client, "http://kibana:5601")
            return _parse_visualization("p1", "Test", "vis-1")

    def test_fixed_interval_extracted(self):
        result = self._parse(self._make_vis_response("fixed_interval", "5m"))
        assert result.date_histogram_interval == "5m"

    def test_calendar_interval_extracted(self):
        result = self._parse(self._make_vis_response("calendar_interval", "1M"))
        assert result.date_histogram_interval == "1M"

    def test_legacy_interval_extracted(self):
        result = self._parse(self._make_vis_response("interval", "1h"))
        assert result.date_histogram_interval == "1h"

    def test_auto_interval_gives_none(self):
        result = self._parse(self._make_vis_response("interval", "auto"))
        assert result.date_histogram_interval is None

    def test_no_interval_gives_none(self):
        result = self._parse(self._make_vis_response())
        assert result.date_histogram_interval is None

    def test_no_date_histogram_gives_none(self):
        aggs = [
            {"type": "count", "schema": "metric", "params": {}},
            {"type": "terms", "schema": "group", "params": {"field": "service"}},
        ]
        vis_response = {
            "attributes": {
                "visState": json.dumps({"type": "line", "aggs": aggs}),
                "title": "Test",
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": json.dumps({
                        "query": {"query": "", "language": "kuery"}, "filter": [],
                    })
                },
            },
            "references": [],
        }
        result = self._parse(vis_response)
        assert result.date_histogram_interval is None


class TestAddRulePanelIncludesDataViewInBatch:
    """Bug 4 prevention: verify data view is in the NDJSON batch."""

    @patch("kibana_connector._import_saved_objects")
    @patch("kibana_connector._fetch_visualization")
    @patch("kibana_connector._resolve_panel_vis_id", return_value="orig-vis-id")
    @patch("kibana_connector.get_dashboard")
    def test_data_view_in_ndjson_batch(
        self, mock_get_dash, mock_resolve, mock_fetch_vis, mock_import
    ):
        from kibana_connector import add_rule_panel_to_dashboard

        mock_get_dash.return_value = {
            "id": "l2m-metrics-dashboard",
            "attributes": {
                "title": "Metrics",
                "panelsJSON": "[]",
                "kibanaSavedObjectMeta": {"searchSourceJSON": "{}"},
            },
            "references": [],
        }

        mock_fetch_vis.return_value = {
            "attributes": {
                "visState": json.dumps({"type": "line", "aggs": [
                    {"type": "count", "schema": "metric", "params": {}},
                ]}),
                "title": "Orig",
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": json.dumps({
                        "query": {"query": "", "language": "kuery"},
                        "filter": [],
                    })
                },
            },
        }
        mock_import.return_value = {"success": True}

        add_rule_panel_to_dashboard(
            rule_id=42,
            rule_name="My Rule",
            origin_dashboard_id="dash-1",
            origin_panel_id="p1",
            compute_type="count",
            compute_field=None,
            dimensions=["service"],
        )

        # Verify _import_saved_objects called with 3 objects:
        # [data_view, visualization, dashboard]
        import_call = mock_import.call_args
        objects = import_call[0][0]
        assert len(objects) == 3

        types = [obj["type"] for obj in objects]
        assert "index-pattern" in types   # data view in batch
        assert "visualization" in types
        assert "dashboard" in types

        # Data view has correct ID
        dv = next(o for o in objects if o["type"] == "index-pattern")
        assert dv["id"] == "l2m-metrics-dv-rule-42"
        assert dv["attributes"]["title"] == "l2m-metrics-rule-42"


class TestImportSavedObjects:
    """Verify NDJSON formatting is correct."""

    @patch("kibana_connector._get_client_and_url")
    def test_ndjson_format(self, mock_get):
        from kibana_connector import _import_saved_objects

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True}
        mock_response.raise_for_status.return_value = None
        mock_client.post.return_value = mock_response
        mock_get.return_value = (mock_client, "http://kibana:5601")

        objects = [
            {"id": "a", "type": "dashboard", "attributes": {"title": "T"}},
            {"id": "b", "type": "visualization", "attributes": {"title": "V"}},
        ]
        _import_saved_objects(objects)

        call_kwargs = mock_client.post.call_args
        # Extract files from the call
        files = call_kwargs.kwargs.get("files") or call_kwargs[1].get("files")
        ndjson_content = files["file"][1].decode()
        lines = ndjson_content.strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["id"] == "a"
        assert json.loads(lines[1])["id"] == "b"

"""Seed Kibana with a data view, saved search, and sample dashboard.

Run after Kibana is healthy:
  python seed.py [--kibana http://localhost:5602]
"""

import argparse
import json
import time

import requests

KIBANA_URL = "http://localhost:5602"
INDEX_PATTERN = "app-logs"
HEADERS = {"kbn-xsrf": "true", "Content-Type": "application/json"}

# Fixed IDs for idempotent seeding
DATA_VIEW_ID = "l2m-app-logs"
SAVED_SEARCH_ID = "l2m-recent-logs"
VIS_ERRORS_ID = "l2m-errors-by-service"
VIS_LATENCY_ID = "l2m-latency-by-endpoint"
DASHBOARD_ID = "l2m-app-overview"


def wait_for_kibana(url: str, retries: int = 30, delay: int = 5):
    for i in range(retries):
        try:
            r = requests.get(f"{url}/api/status", timeout=5)
            if r.status_code == 200:
                print("Kibana is ready.")
                return
        except requests.ConnectionError:
            pass
        print(f"Waiting for Kibana... ({i+1}/{retries})")
        time.sleep(delay)
    raise RuntimeError("Kibana not ready")


def create_data_view(url: str):
    """Create a Kibana data view for app-logs."""
    payload = {
        "data_view": {
            "id": DATA_VIEW_ID,
            "title": INDEX_PATTERN,
            "timeFieldName": "timestamp",
            "name": "App Logs",
        }
    }
    r = requests.post(f"{url}/api/data_views/data_view", headers=HEADERS, json=payload)
    if r.status_code == 200:
        print(f"Created data view: {DATA_VIEW_ID}")
        return DATA_VIEW_ID
    if r.status_code in (400, 409):
        print(f"Data view already exists: {DATA_VIEW_ID}")
        return DATA_VIEW_ID
    print(f"Data view creation failed: {r.status_code} {r.text}")
    return None


def import_objects(url: str):
    """Import dashboard + visualizations via NDJSON import API."""

    # --- Saved Search: Recent log lines (POOR metric candidate) ---
    saved_search = {
        "id": SAVED_SEARCH_ID,
        "type": "search",
        "attributes": {
            "title": "Recent log lines",
            "description": "Raw log documents - not suitable for metric conversion",
            "columns": ["timestamp", "service", "level", "endpoint", "status_code", "message"],
            "sort": [["timestamp", "desc"]],
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"query": "", "language": "kuery"},
                    "filter": [],
                    "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                }),
            },
        },
        "references": [
            {"id": DATA_VIEW_ID, "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}
        ],
    }

    # --- Visualization 1: Errors/min by service (GOOD metric candidate) ---
    vis_errors_state = {
        "type": "line",
        "params": {
            "type": "line",
            "grid": {"categoryLines": False},
            "categoryAxes": [{
                "id": "CategoryAxis-1", "type": "category", "position": "bottom",
                "show": True, "style": {}, "scale": {"type": "linear"},
                "labels": {"show": True, "filter": True, "truncate": 100},
                "title": {},
            }],
            "valueAxes": [{
                "id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value", "position": "left",
                "show": True, "style": {}, "scale": {"type": "linear", "mode": "normal"},
                "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                "title": {"text": "Count"},
            }],
            "seriesParams": [{"show": True, "type": "line", "mode": "normal", "data": {"label": "Count", "id": "1"}, "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True, "lineWidth": 2, "showCircles": True}],
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "times": [], "addTimeMarker": False,
        },
        "aggs": [
            {"id": "1", "enabled": True, "type": "count", "params": {}, "schema": "metric"},
            {
                "id": "2", "enabled": True, "type": "date_histogram", "params": {
                    "field": "timestamp", "interval": "auto", "min_doc_count": 1,
                    "extended_bounds": {},
                },
                "schema": "segment",
            },
            {
                "id": "3", "enabled": True, "type": "terms", "params": {
                    "field": "service", "size": 10, "order": "desc", "orderBy": "1",
                },
                "schema": "group",
            },
        ],
    }

    vis_errors = {
        "id": VIS_ERRORS_ID,
        "type": "visualization",
        "attributes": {
            "title": "Errors/min by service",
            "description": "Error count over time grouped by service (status >= 500)",
            "visState": json.dumps(vis_errors_state),
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"query": "status_code >= 500", "language": "kuery"},
                    "filter": [],
                    "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                }),
            },
        },
        "references": [
            {"id": DATA_VIEW_ID, "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}
        ],
    }

    # --- Visualization 2: Avg latency by endpoint (GOOD metric candidate) ---
    vis_latency_state = {
        "type": "line",
        "params": {
            "type": "line",
            "grid": {"categoryLines": False},
            "categoryAxes": [{
                "id": "CategoryAxis-1", "type": "category", "position": "bottom",
                "show": True, "style": {}, "scale": {"type": "linear"},
                "labels": {"show": True, "filter": True, "truncate": 100},
                "title": {},
            }],
            "valueAxes": [{
                "id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value", "position": "left",
                "show": True, "style": {}, "scale": {"type": "linear", "mode": "normal"},
                "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                "title": {"text": "Avg response_time_ms"},
            }],
            "seriesParams": [{"show": True, "type": "line", "mode": "normal", "data": {"label": "Avg response_time_ms", "id": "1"}, "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True, "lineWidth": 2, "showCircles": True}],
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "times": [], "addTimeMarker": False,
        },
        "aggs": [
            {"id": "1", "enabled": True, "type": "avg", "params": {"field": "response_time_ms"}, "schema": "metric"},
            {
                "id": "2", "enabled": True, "type": "date_histogram", "params": {
                    "field": "timestamp", "interval": "auto", "min_doc_count": 1,
                    "extended_bounds": {},
                },
                "schema": "segment",
            },
            {
                "id": "3", "enabled": True, "type": "terms", "params": {
                    "field": "endpoint", "size": 10, "order": "desc", "orderBy": "1",
                },
                "schema": "group",
            },
        ],
    }

    vis_latency = {
        "id": VIS_LATENCY_ID,
        "type": "visualization",
        "attributes": {
            "title": "Avg latency by endpoint",
            "description": "Average response time over time grouped by endpoint",
            "visState": json.dumps(vis_latency_state),
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"query": "", "language": "kuery"},
                    "filter": [],
                    "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                }),
            },
        },
        "references": [
            {"id": DATA_VIEW_ID, "name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern"}
        ],
    }

    # --- Dashboard ---
    panels = [
        {
            "panelIndex": "p1",
            "gridData": {"x": 0, "y": 0, "w": 24, "h": 15, "i": "p1"},
            "type": "visualization",
            "panelRefName": "panel_p1",
            "title": "Errors/min by service",
        },
        {
            "panelIndex": "p2",
            "gridData": {"x": 24, "y": 0, "w": 24, "h": 15, "i": "p2"},
            "type": "visualization",
            "panelRefName": "panel_p2",
            "title": "Avg latency by endpoint",
        },
        {
            "panelIndex": "p3",
            "gridData": {"x": 0, "y": 15, "w": 48, "h": 15, "i": "p3"},
            "type": "search",
            "panelRefName": "panel_p3",
            "title": "Recent log lines",
        },
    ]

    dashboard = {
        "id": DASHBOARD_ID,
        "type": "dashboard",
        "attributes": {
            "title": "App Service Overview",
            "description": "Sample dashboard with panels of varying metric suitability",
            "panelsJSON": json.dumps(panels),
            "timeRestore": True,
            "timeTo": "now",
            "timeFrom": "now-24h",
            "refreshInterval": {"pause": False, "value": 30000},
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps({
                    "query": {"query": "", "language": "kuery"},
                    "filter": [],
                }),
            },
        },
        "references": [
            {"id": VIS_ERRORS_ID, "name": "panel_p1", "type": "visualization"},
            {"id": VIS_LATENCY_ID, "name": "panel_p2", "type": "visualization"},
            {"id": SAVED_SEARCH_ID, "name": "panel_p3", "type": "search"},
        ],
    }

    # Build NDJSON payload
    objects = [saved_search, vis_errors, vis_latency, dashboard]
    ndjson = "\n".join(json.dumps(obj) for obj in objects) + "\n"

    r = requests.post(
        f"{url}/api/saved_objects/_import?overwrite=true",
        headers={"kbn-xsrf": "true"},
        files={"file": ("objects.ndjson", ndjson, "application/ndjson")},
    )
    if r.status_code == 200:
        result = r.json()
        if result.get("success"):
            print(f"Imported {result['successCount']} objects successfully.")
            print(f"  Dashboard: {url}/app/dashboards#/view/{DASHBOARD_ID}")
        else:
            print(f"Import partial: {result}")
    else:
        print(f"Import failed: {r.status_code} {r.text}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kibana", default=KIBANA_URL)
    args = parser.parse_args()

    wait_for_kibana(args.kibana)
    dv_id = create_data_view(args.kibana)
    if dv_id:
        import_objects(args.kibana)
    print("Done.")


if __name__ == "__main__":
    main()

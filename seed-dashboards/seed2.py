"""Seed Kibana 2 (auth-enabled) with a different dashboard.

Run after Kibana2 is healthy:
  python seed2.py --kibana http://localhost:5603 --user elastic --password admin
"""

import argparse
import json
import time

import requests

KIBANA_URL = "http://localhost:5603"
INDEX_PATTERN = "app-logs"

# Fixed IDs — l2m2 prefix to avoid collision with primary Kibana
DATA_VIEW_ID = "l2m2-app-logs"
VIS_REQ_RATE_ID = "l2m2-request-rate-by-tenant"
VIS_SUM_RT_ID = "l2m2-sum-rt-by-service"
VIS_STATUS_ID = "l2m2-status-code-breakdown"
DASHBOARD_ID = "l2m2-tenant-ops"


def wait_for_kibana(url: str, auth=None, retries: int = 30, delay: int = 5):
    for i in range(retries):
        try:
            r = requests.get(f"{url}/api/status", timeout=5, auth=auth)
            if r.status_code == 200:
                print("Kibana is ready.")
                return
        except requests.ConnectionError:
            pass
        print(f"Waiting for Kibana... ({i+1}/{retries})")
        time.sleep(delay)
    raise RuntimeError("Kibana not ready")


def create_data_view(url: str, auth=None):
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    payload = {
        "data_view": {
            "id": DATA_VIEW_ID,
            "title": INDEX_PATTERN,
            "timeFieldName": "timestamp",
            "name": "App Logs (Kibana 2)",
        }
    }
    r = requests.post(f"{url}/api/data_views/data_view", headers=headers, json=payload, auth=auth)
    if r.status_code == 200:
        print(f"Created data view: {DATA_VIEW_ID}")
        return DATA_VIEW_ID
    if r.status_code in (400, 409):
        print(f"Data view already exists: {DATA_VIEW_ID}")
        return DATA_VIEW_ID
    print(f"Data view creation failed: {r.status_code} {r.text}")
    return None


def import_objects(url: str, auth=None):
    # --- Visualization 1: Request rate by tenant (GOOD candidate — count + date_histogram + terms) ---
    vis_req_rate_state = {
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
                "title": {"text": "Request Count"},
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
                    "field": "tenant", "size": 10, "order": "desc", "orderBy": "1",
                },
                "schema": "group",
            },
        ],
    }

    vis_req_rate = {
        "id": VIS_REQ_RATE_ID,
        "type": "visualization",
        "attributes": {
            "title": "Request rate by tenant",
            "description": "Request count over time grouped by tenant",
            "visState": json.dumps(vis_req_rate_state),
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

    # --- Visualization 2: Sum response time by service (GOOD candidate — sum + date_histogram + terms) ---
    vis_sum_rt_state = {
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
                "title": {"text": "Sum response_time_ms"},
            }],
            "seriesParams": [{"show": True, "type": "line", "mode": "normal", "data": {"label": "Sum response_time_ms", "id": "1"}, "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True, "lineWidth": 2, "showCircles": True}],
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "times": [], "addTimeMarker": False,
        },
        "aggs": [
            {"id": "1", "enabled": True, "type": "sum", "params": {"field": "response_time_ms"}, "schema": "metric"},
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

    vis_sum_rt = {
        "id": VIS_SUM_RT_ID,
        "type": "visualization",
        "attributes": {
            "title": "Sum response time by service",
            "description": "Total response time over time grouped by service",
            "visState": json.dumps(vis_sum_rt_state),
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

    # --- Visualization 3: Status code breakdown (MEDIUM candidate — terms without date_histogram) ---
    vis_status_state = {
        "type": "pie",
        "params": {
            "type": "pie",
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "isDonut": False,
            "labels": {"show": True, "values": True, "last_level": True, "truncate": 100},
        },
        "aggs": [
            {"id": "1", "enabled": True, "type": "count", "params": {}, "schema": "metric"},
            {
                "id": "2", "enabled": True, "type": "terms", "params": {
                    "field": "status_code", "size": 10, "order": "desc", "orderBy": "1",
                },
                "schema": "segment",
            },
        ],
    }

    vis_status = {
        "id": VIS_STATUS_ID,
        "type": "visualization",
        "attributes": {
            "title": "Status code breakdown",
            "description": "Distribution of HTTP status codes (no time axis)",
            "visState": json.dumps(vis_status_state),
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
            "title": "Request rate by tenant",
        },
        {
            "panelIndex": "p2",
            "gridData": {"x": 24, "y": 0, "w": 24, "h": 15, "i": "p2"},
            "type": "visualization",
            "panelRefName": "panel_p2",
            "title": "Sum response time by service",
        },
        {
            "panelIndex": "p3",
            "gridData": {"x": 0, "y": 15, "w": 48, "h": 15, "i": "p3"},
            "type": "visualization",
            "panelRefName": "panel_p3",
            "title": "Status code breakdown",
        },
    ]

    dashboard = {
        "id": DASHBOARD_ID,
        "type": "dashboard",
        "attributes": {
            "title": "Tenant Operations Dashboard",
            "description": "Second dashboard with different panel shapes for multi-Kibana testing",
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
            {"id": VIS_REQ_RATE_ID, "name": "panel_p1", "type": "visualization"},
            {"id": VIS_SUM_RT_ID, "name": "panel_p2", "type": "visualization"},
            {"id": VIS_STATUS_ID, "name": "panel_p3", "type": "visualization"},
        ],
    }

    # Build NDJSON payload
    objects = [vis_req_rate, vis_sum_rt, vis_status, dashboard]
    ndjson = "\n".join(json.dumps(obj) for obj in objects) + "\n"

    r = requests.post(
        f"{url}/api/saved_objects/_import?overwrite=true",
        headers={"kbn-xsrf": "true"},
        files={"file": ("objects.ndjson", ndjson, "application/ndjson")},
        auth=auth,
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
    parser.add_argument("--user", default=None, help="Kibana username (e.g. elastic)")
    parser.add_argument("--password", default=None, help="Kibana password")
    args = parser.parse_args()

    auth = None
    if args.user and args.password:
        auth = (args.user, args.password)

    wait_for_kibana(args.kibana, auth=auth)
    dv_id = create_data_view(args.kibana, auth=auth)
    if dv_id:
        import_objects(args.kibana, auth=auth)
    print("Done.")


if __name__ == "__main__":
    main()

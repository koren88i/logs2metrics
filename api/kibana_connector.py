"""Kibana connector.

Reads dashboards/saved objects and creates metrics dashboards via the
Kibana REST API.
Supports optional per-request connection override (URL + basic auth).
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

from config import KIBANA_URL
from connector_models import (
    DashboardDetail,
    DashboardSummary,
    MetricInfo,
    PanelAnalysis,
)

HEADERS = {"kbn-xsrf": "true"}

# Default client for the docker-compose Kibana (no auth)
_default_client = httpx.Client(headers=HEADERS, follow_redirects=True)


@dataclass
class KibanaConnection:
    """Optional override for Kibana URL + basic-auth credentials."""

    url: str
    username: str | None = None
    password: str | None = None


def _get_client_and_url(
    conn: KibanaConnection | None,
) -> tuple[httpx.Client, str]:
    """Return (httpx_client, base_url) for the given connection or defaults."""
    if conn is None:
        return _default_client, KIBANA_URL
    auth = None
    if conn.username and conn.password:
        auth = httpx.BasicAuth(conn.username, conn.password)
    client = httpx.Client(headers=HEADERS, follow_redirects=True, auth=auth)
    return client, conn.url


# ── Public API ────────────────────────────────────────────────────────


def list_dashboards(
    conn: KibanaConnection | None = None,
) -> list[DashboardSummary]:
    """Return all dashboards with id, title, description."""
    client, base_url = _get_client_and_url(conn)
    response = client.get(
        f"{base_url}/api/saved_objects/_find",
        params={"type": "dashboard", "per_page": 100},
    )
    response.raise_for_status()
    data = response.json()
    return [
        DashboardSummary(
            id=obj["id"],
            title=obj["attributes"].get("title", ""),
            description=obj["attributes"].get("description", ""),
        )
        for obj in data.get("saved_objects", [])
    ]


def get_dashboard(
    dashboard_id: str,
    conn: KibanaConnection | None = None,
) -> dict:
    """Return the full saved object for a dashboard."""
    client, base_url = _get_client_and_url(conn)
    response = client.get(
        f"{base_url}/api/saved_objects/dashboard/{dashboard_id}",
    )
    response.raise_for_status()
    return response.json()


def get_dashboard_with_panels(
    dashboard_id: str,
    conn: KibanaConnection | None = None,
) -> DashboardDetail:
    """Fetch a dashboard and parse all its panels into PanelAnalysis objects."""
    dashboard = get_dashboard(dashboard_id, conn=conn)
    attrs = dashboard["attributes"]

    panels_json = json.loads(attrs.get("panelsJSON", "[]"))
    references = {ref["name"]: ref for ref in dashboard.get("references", [])}

    panel_analyses = []
    for panel in panels_json:
        panel_ref_name = panel.get("panelRefName", "")
        panel_index = panel.get("panelIndex", "")
        # Kibana may prefix reference names with "{panelIndex}:"
        ref = (
            references.get(panel_ref_name)
            or references.get(f"{panel_index}:{panel_ref_name}")
            or {}
        )
        ref_id = ref.get("id", "")
        ref_type = ref.get("type", panel.get("type", ""))

        analysis = _resolve_and_parse_panel(panel, ref_id, ref_type, conn=conn)
        panel_analyses.append(analysis)

    return DashboardDetail(
        id=dashboard["id"],
        title=attrs.get("title", ""),
        description=attrs.get("description", ""),
        panels=panel_analyses,
    )


def get_data_view_index_pattern(
    data_view_id: str,
    conn: KibanaConnection | None = None,
) -> str | None:
    """Resolve a Kibana data view ID to its ES index pattern string."""
    client, base_url = _get_client_and_url(conn)
    response = client.get(
        f"{base_url}/api/data_views/data_view/{data_view_id}",
    )
    if response.status_code != 200:
        return None
    return response.json().get("data_view", {}).get("title")


# ── Internal helpers ──────────────────────────────────────────────────


def _resolve_and_parse_panel(
    panel: dict,
    ref_id: str,
    ref_type: str,
    conn: KibanaConnection | None = None,
) -> PanelAnalysis:
    """Fetch referenced saved object and parse it into a PanelAnalysis."""
    panel_id = panel.get("panelIndex", "")
    panel_title = panel.get("title", "")

    if ref_type == "search":
        return _parse_saved_search(panel_id, panel_title, ref_id, conn=conn)
    elif ref_type == "visualization":
        return _parse_visualization(panel_id, panel_title, ref_id, conn=conn)
    else:
        return PanelAnalysis(
            panel_id=panel_id,
            title=panel_title,
            visualization_type=ref_type or "unknown",
        )


def _parse_saved_search(
    panel_id: str,
    title: str,
    search_id: str,
    conn: KibanaConnection | None = None,
) -> PanelAnalysis:
    """Parse a saved search (always has_raw_docs=True, no aggs)."""
    client, base_url = _get_client_and_url(conn)
    response = client.get(
        f"{base_url}/api/saved_objects/search/{search_id}",
    )
    response.raise_for_status()
    obj = response.json()
    attrs = obj["attributes"]
    refs = obj.get("references", [])

    index_pattern = _extract_index_from_refs(refs, conn=conn)

    search_source = json.loads(
        attrs.get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
    )
    filter_query = _extract_query_string(search_source)

    return PanelAnalysis(
        panel_id=panel_id,
        title=title or attrs.get("title", ""),
        index_pattern=index_pattern,
        visualization_type="search",
        has_raw_docs=True,
        filter_query=filter_query,
    )


def _parse_visualization(
    panel_id: str,
    title: str,
    vis_id: str,
    conn: KibanaConnection | None = None,
) -> PanelAnalysis:
    """Fetch a visualization saved object and parse its visState aggs."""
    client, base_url = _get_client_and_url(conn)
    response = client.get(
        f"{base_url}/api/saved_objects/visualization/{vis_id}",
    )
    response.raise_for_status()
    obj = response.json()
    attrs = obj["attributes"]
    refs = obj.get("references", [])

    index_pattern = _extract_index_from_refs(refs, conn=conn)

    search_source = json.loads(
        attrs.get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
    )
    filter_query = _extract_query_string(search_source)

    vis_state = json.loads(attrs.get("visState", "{}"))
    vis_type = vis_state.get("type", "unknown")
    aggs = vis_state.get("aggs", [])

    agg_types = []
    metrics = []
    group_by_fields = []
    time_field = None

    for agg in aggs:
        if not agg.get("enabled", True):
            continue

        agg_type = agg.get("type", "")
        schema = agg.get("schema", "")
        params = agg.get("params", {})

        agg_types.append(agg_type)

        if schema == "metric":
            metric_field = params.get("field")
            metrics.append(MetricInfo(type=agg_type, field=metric_field))
        elif schema == "segment":
            if agg_type == "date_histogram":
                time_field = params.get("field", "timestamp")
        elif schema == "group":
            field = params.get("field")
            if field:
                group_by_fields.append(field)

    return PanelAnalysis(
        panel_id=panel_id,
        title=title or attrs.get("title", ""),
        index_pattern=index_pattern,
        time_field=time_field,
        visualization_type=vis_type,
        agg_types=agg_types,
        metrics=metrics,
        group_by_fields=group_by_fields,
        has_raw_docs=False,
        filter_query=filter_query,
    )


def _extract_index_from_refs(
    references: list[dict],
    conn: KibanaConnection | None = None,
) -> str | None:
    """Find the ES index pattern from a saved object's references.

    References contain a data view ID, not the actual ES index name.
    Resolve via the Kibana data views API, falling back to the raw ID.
    """
    for ref in references:
        if ref.get("type") == "index-pattern":
            data_view_id = ref.get("id")
            resolved = get_data_view_index_pattern(data_view_id, conn=conn)
            return resolved or data_view_id
    return None


def _extract_query_string(search_source: dict) -> str | None:
    """Extract the KQL/Lucene query string from a searchSourceJSON dict."""
    query = search_source.get("query", {})
    query_str = query.get("query", "")
    if query_str and query_str.strip():
        return query_str.strip()
    return None


# ── Metrics Dashboard (write operations) ─────────────────────────────

METRICS_DASHBOARD_ID = "l2m-metrics-dashboard"
METRICS_VIS_PREFIX = "l2m-metrics-vis-rule-"
METRICS_DV_PREFIX = "l2m-metrics-dv-rule-"

# Metric agg mapping: compute_type -> (agg_type, field_name_template)
_METRIC_AGG_MAP = {
    "count": ("sum", "doc_count"),
    "sum": ("sum", "sum_{field}"),
    "avg": ("avg", "avg_{field}"),
    "distribution": ("avg", "pct_{field}"),
}


def create_metrics_dashboard(
    title: str,
    conn: KibanaConnection | None = None,
) -> dict:
    """Create an empty Kibana metrics dashboard via NDJSON import."""
    dashboard_obj = {
        "id": METRICS_DASHBOARD_ID,
        "type": "dashboard",
        "attributes": {
            "title": title,
            "description": "Metrics dashboard created by Logs2Metrics",
            "panelsJSON": json.dumps([]),
            "timeRestore": True,
            "timeTo": "now",
            "timeFrom": "now-24h",
            "refreshInterval": {"pause": False, "value": 30000},
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}
                ),
            },
        },
        "references": [],
    }
    return _import_saved_objects([dashboard_obj], conn=conn)


def get_metrics_dashboard(
    conn: KibanaConnection | None = None,
) -> dict | None:
    """Fetch the metrics dashboard if it exists. Returns None if not found."""
    try:
        return get_dashboard(METRICS_DASHBOARD_ID, conn=conn)
    except httpx.HTTPStatusError:
        return None


def add_rule_panel_to_dashboard(
    rule_id: int,
    rule_name: str,
    origin_dashboard_id: str,
    origin_panel_id: str,
    compute_type: str,
    compute_field: str | None,
    dimensions: list[str],
    time_field: str = "timestamp",
    conn: KibanaConnection | None = None,
) -> dict:
    """Add a rule's visualization to the metrics dashboard.

    Clones the original visualization (resolved from the origin dashboard
    and panel index), rewires the metric agg to read from the pre-computed
    metrics index, and appends it as a panel to the existing metrics dashboard.
    """
    # 1. Fetch current dashboard
    dashboard = get_dashboard(METRICS_DASHBOARD_ID, conn=conn)
    attrs = dashboard["attributes"]
    existing_panels = json.loads(attrs.get("panelsJSON", "[]"))
    existing_refs = dashboard.get("references", [])

    # 2. Derive IDs
    dv_id = f"{METRICS_DV_PREFIX}{rule_id}"
    vis_id = f"{METRICS_VIS_PREFIX}{rule_id}"
    index_pattern = f"l2m-metrics-rule-{rule_id}"

    # 3. Create data view for metrics index
    _create_data_view(dv_id, index_pattern, time_field, f"Metrics: {rule_name}", conn=conn)

    # 4. Resolve the original visualization ID from the origin dashboard
    origin_vis_id = _resolve_panel_vis_id(origin_dashboard_id, origin_panel_id, conn=conn)
    if not origin_vis_id:
        raise ValueError(
            f"Could not resolve visualization for panel '{origin_panel_id}' "
            f"in dashboard '{origin_dashboard_id}'"
        )

    # 5. Fetch & clone the original visualization
    original_vis = _fetch_visualization(origin_vis_id, conn=conn)
    vis_obj = _clone_and_rewire_visualization(
        original_vis, vis_id, dv_id, rule_name, compute_type, compute_field
    )

    # 6. Compute panel position (stack vertically, full width)
    panel_index = f"p_rule_{rule_id}"
    row = len(existing_panels)
    new_panel = {
        "panelIndex": panel_index,
        "gridData": {"x": 0, "y": row * 15, "w": 48, "h": 15, "i": panel_index},
        "type": "visualization",
        "panelRefName": f"panel_{panel_index}",
        "title": rule_name,
    }

    # 7. Build updated dashboard
    updated_panels = existing_panels + [new_panel]
    updated_refs = existing_refs + [
        {"id": vis_id, "name": f"panel_{panel_index}", "type": "visualization"}
    ]
    dashboard_obj = {
        "id": METRICS_DASHBOARD_ID,
        "type": "dashboard",
        "attributes": {
            **attrs,
            "panelsJSON": json.dumps(updated_panels),
        },
        "references": updated_refs,
    }

    # 8. Import visualization + updated dashboard
    return _import_saved_objects([vis_obj, dashboard_obj], conn=conn)


# ── Write helpers ────────────────────────────────────────────────────


def _resolve_panel_vis_id(
    dashboard_id: str,
    panel_index: str,
    conn: KibanaConnection | None = None,
) -> str | None:
    """Resolve a dashboard panel index to its visualization saved object ID.

    Fetches the dashboard, finds the panel by panelIndex, then looks up
    the visualization ID from the dashboard's references array.
    """
    dashboard = get_dashboard(dashboard_id, conn=conn)
    panels = json.loads(dashboard["attributes"].get("panelsJSON", "[]"))
    references = {ref["name"]: ref for ref in dashboard.get("references", [])}

    for panel in panels:
        if panel.get("panelIndex") == panel_index:
            ref_name = panel.get("panelRefName", "")
            # Kibana may prefix reference names with "{panelIndex}:"
            ref = (
                references.get(ref_name)
                or references.get(f"{panel_index}:{ref_name}")
            )
            if ref:
                return ref.get("id")
    return None


def _import_saved_objects(
    objects: list[dict],
    conn: KibanaConnection | None = None,
) -> dict:
    """Import saved objects via Kibana NDJSON import API (with overwrite)."""
    client, base_url = _get_client_and_url(conn)
    ndjson = "\n".join(json.dumps(obj) for obj in objects) + "\n"
    response = client.post(
        f"{base_url}/api/saved_objects/_import",
        params={"overwrite": "true"},
        files={"file": ("objects.ndjson", ndjson.encode(), "application/ndjson")},
    )
    response.raise_for_status()
    return response.json()


def _create_data_view(
    dv_id: str,
    index_pattern: str,
    time_field: str,
    name: str,
    conn: KibanaConnection | None = None,
) -> str:
    """Create a Kibana data view. Ignores 400 (already exists)."""
    client, base_url = _get_client_and_url(conn)
    payload = {
        "data_view": {
            "id": dv_id,
            "title": index_pattern,
            "timeFieldName": time_field,
            "name": name,
        }
    }
    response = client.post(
        f"{base_url}/api/data_views/data_view",
        json=payload,
    )
    if response.status_code not in (200, 400, 409):
        response.raise_for_status()
    return dv_id


def _fetch_visualization(
    vis_id: str,
    conn: KibanaConnection | None = None,
) -> dict:
    """Fetch a visualization saved object by ID."""
    client, base_url = _get_client_and_url(conn)
    response = client.get(
        f"{base_url}/api/saved_objects/visualization/{vis_id}",
    )
    response.raise_for_status()
    return response.json()


def _clone_and_rewire_visualization(
    original_vis: dict,
    new_vis_id: str,
    new_dv_id: str,
    title: str,
    compute_type: str,
    compute_field: str | None,
) -> dict:
    """Clone an original visualization and rewire it for the metrics index.

    Keeps the visualization type, display params, date_histogram and terms
    aggs. Replaces the metric agg to read from the pre-computed field.
    Updates the data view reference.
    """
    attrs = copy.deepcopy(original_vis.get("attributes", {}))

    # Parse and modify visState
    vis_state = json.loads(attrs.get("visState", "{}"))
    aggs = vis_state.get("aggs", [])

    # Replace metric aggs
    agg_type, field_template = _METRIC_AGG_MAP.get(compute_type, ("sum", "doc_count"))
    metric_field = field_template.replace("{field}", compute_field or "")

    for agg in aggs:
        if agg.get("schema") == "metric":
            agg["type"] = agg_type
            agg["params"] = {"field": metric_field}

    vis_state["aggs"] = aggs
    attrs["visState"] = json.dumps(vis_state)
    attrs["title"] = title

    # Update searchSourceJSON to reference new data view
    search_source = json.loads(
        attrs.get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
    )
    # Clear any filter from the original (metrics index is already filtered)
    search_source["query"] = {"query": "", "language": "kuery"}
    search_source["filter"] = []
    attrs.setdefault("kibanaSavedObjectMeta", {})["searchSourceJSON"] = json.dumps(
        search_source
    )

    return {
        "id": new_vis_id,
        "type": "visualization",
        "attributes": attrs,
        "references": [
            {
                "id": new_dv_id,
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
            }
        ],
    }

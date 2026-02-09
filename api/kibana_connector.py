"""Kibana read-only connector.

Reads dashboards and saved objects via the Kibana REST API.
Supports optional per-request connection override (URL + basic auth).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

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

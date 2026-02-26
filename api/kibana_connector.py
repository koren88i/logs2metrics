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

    # Extract fields from dashboard-level control panels
    dashboard_filter_fields = _extract_control_panel_fields(panels_json)

    # Extract fields from dashboard-level searchSourceJSON filters
    dashboard_search_source = json.loads(
        attrs.get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
    )
    dashboard_filter_fields.extend(_extract_filter_fields(dashboard_search_source))
    dashboard_filter_fields = list(dict.fromkeys(dashboard_filter_fields))

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
        dashboard_filter_fields=dashboard_filter_fields,
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
    elif ref_type == "lens":
        return _parse_lens_panel(panel, conn=conn)
    else:
        # Try embedded panel (by-value) — Kibana 8 stores full config inline
        embedded = panel.get("embeddableConfig", {}).get("attributes")
        if embedded:
            return _parse_embedded_panel(panel, conn=conn)
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
    filter_fields = _extract_filter_fields(search_source)

    return PanelAnalysis(
        panel_id=panel_id,
        title=title or attrs.get("title", ""),
        index_pattern=index_pattern,
        visualization_type="search",
        has_raw_docs=True,
        filter_query=filter_query,
        filter_fields=filter_fields,
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

    index_pattern, data_view_time_field = _extract_index_and_time_field_from_refs(refs, conn=conn)

    search_source = json.loads(
        attrs.get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
    )
    filter_query = _extract_query_string(search_source)
    filter_fields = _extract_filter_fields(search_source)

    vis_state = json.loads(attrs.get("visState", "{}"))
    vis_type = vis_state.get("type", "unknown")
    aggs = vis_state.get("aggs", [])

    agg_types = []
    metrics = []
    group_by_fields = []
    time_field = None
    date_histogram_interval = None

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
                # Extract the fixed or calendar interval configured on the panel.
                # Kibana stores it as "fixed_interval" (e.g. "1m") or "interval"
                # (legacy format, e.g. "1m", "auto"). "auto" means Kibana picks
                # dynamically based on the time range — we can't pre-fill that.
                raw_interval = (
                    params.get("fixed_interval")
                    or params.get("calendar_interval")
                    or params.get("interval")
                )
                if raw_interval and raw_interval != "auto":
                    date_histogram_interval = raw_interval
            else:
                # Pie/bar charts use "segment" for terms slices
                field = params.get("field")
                if field:
                    group_by_fields.append(field)
        elif schema in ("group", "bucket"):
            # "group" = split series in charts, "bucket" = row grouping in tables
            field = params.get("field")
            if field:
                group_by_fields.append(field)

    es_query = _build_es_query_from_vis_aggs(aggs, filter_query) if aggs else None

    # For panels without date_histogram, fall back to the data view's time field
    if not time_field and data_view_time_field:
        time_field = data_view_time_field

    return PanelAnalysis(
        panel_id=panel_id,
        title=title or attrs.get("title", ""),
        index_pattern=index_pattern,
        time_field=time_field,
        date_histogram_interval=date_histogram_interval,
        visualization_type=vis_type,
        agg_types=agg_types,
        metrics=metrics,
        group_by_fields=group_by_fields,
        has_raw_docs=False,
        filter_query=filter_query,
        filter_fields=filter_fields,
        es_query=es_query,
    )


def _parse_lens_panel(
    panel: dict,
    conn: KibanaConnection | None = None,
) -> PanelAnalysis:
    """Parse a Lens panel (by-value) from its embedded config.

    Lens panels store their full state inside
    panel["embeddableConfig"]["attributes"] including references to data views
    and column definitions describing aggregations.
    """
    panel_id = panel.get("panelIndex", "")
    panel_title = panel.get("title", "")
    embedded = panel.get("embeddableConfig", {}).get("attributes", {})

    # Resolve index pattern and time field from embedded references
    refs = embedded.get("references", [])
    index_pattern, data_view_time_field = _extract_index_and_time_field_from_refs(refs, conn=conn)

    vis_type = embedded.get("visualizationType", "lens")
    state = embedded.get("state", {})

    # Extract agg info from Lens column definitions
    agg_types = []
    metrics = []
    group_by_fields = []
    time_field = None
    date_histogram_interval = None

    datasource_states = state.get("datasourceStates", {})
    form_based = datasource_states.get("formBased") or datasource_states.get("indexpattern", {})
    layers = form_based.get("layers", {})

    # Collect all columns across layers for ES query building
    all_columns = {}
    for layer in layers.values():
        columns = layer.get("columns", {})
        all_columns.update(columns)
        for col in columns.values():
            op = col.get("operationType", "")
            source_field = col.get("sourceField")
            params = col.get("params", {})

            if op == "date_histogram":
                agg_types.append("date_histogram")
                if source_field:
                    time_field = source_field
                raw_interval = params.get("interval")
                if raw_interval and raw_interval != "auto":
                    date_histogram_interval = raw_interval
            elif op in ("count", "sum", "average", "min", "max", "median",
                        "percentile", "unique_count", "last_value",
                        "cumulative_sum", "counter_rate", "differences"):
                agg_types.append(op)
                metrics.append(MetricInfo(
                    type=op,
                    field=source_field if op != "count" else None,
                ))
            elif op == "terms":
                agg_types.append("terms")
                if source_field:
                    group_by_fields.append(source_field)
            elif op == "filters":
                agg_types.append("filters")

    # Extract filter query from Lens state
    query = state.get("query", {})
    query_str = query.get("query", "")
    filter_query = query_str.strip() if query_str and query_str.strip() else None

    # Lens stores structured filters separately from the query
    lens_filters = state.get("filters", [])
    filter_fields = _extract_filter_fields({"filter": lens_filters})

    # Build ES query from the raw Lens columns (preserves sizes, ordering, nesting)
    es_query = _build_es_query_from_lens_columns(all_columns, filter_query) if all_columns else None

    # For panels without date_histogram (e.g. tables), fall back to the
    # data view's time field so transforms can still bucket by time.
    if not time_field and data_view_time_field:
        time_field = data_view_time_field

    return PanelAnalysis(
        panel_id=panel_id,
        title=panel_title or embedded.get("title", ""),
        index_pattern=index_pattern,
        time_field=time_field,
        date_histogram_interval=date_histogram_interval,
        visualization_type=vis_type,
        agg_types=agg_types,
        metrics=metrics,
        group_by_fields=group_by_fields,
        has_raw_docs=False,
        filter_query=filter_query,
        filter_fields=filter_fields,
        es_query=es_query,
    )


def _parse_embedded_panel(
    panel: dict,
    conn: KibanaConnection | None = None,
) -> PanelAnalysis:
    """Parse a generic embedded (by-value) panel.

    Extracts the index pattern from embedded references but does not attempt
    to parse aggregation details for non-Lens panel types (TSVB, maps, etc.).
    """
    panel_id = panel.get("panelIndex", "")
    panel_title = panel.get("title", "")
    embedded = panel.get("embeddableConfig", {}).get("attributes", {})

    refs = embedded.get("references", [])
    index_pattern, data_view_time_field = _extract_index_and_time_field_from_refs(refs, conn=conn)

    panel_type = panel.get("type", "unknown")

    return PanelAnalysis(
        panel_id=panel_id,
        title=panel_title or embedded.get("title", ""),
        index_pattern=index_pattern,
        time_field=data_view_time_field,
        visualization_type=panel_type,
    )


def _build_es_query_from_vis_aggs(
    aggs: list[dict],
    filter_query: str | None,
) -> dict:
    """Build an ES query body from a legacy visualization's visState.aggs.

    Preserves the original aggregation structure: date_histogram buckets,
    terms buckets with their configured sizes, and metric aggregations with
    their original field references.
    """
    es_filter = (
        {"query_string": {"query": filter_query}} if filter_query
        else {"match_all": {}}
    )

    # Separate aggs by schema role
    segment_aggs = []  # date_histogram, terms used as x-axis segments
    group_aggs = []    # terms used for grouping (split series)
    metric_aggs = []   # count, avg, sum, etc.

    for agg in aggs:
        if not agg.get("enabled", True):
            continue
        schema = agg.get("schema", "")
        agg_type = agg.get("type", "")
        if schema == "segment" and agg_type == "date_histogram":
            segment_aggs.append(agg)
        elif schema in ("segment", "group", "bucket") and agg_type != "date_histogram":
            # "segment" non-date_histogram = pie/bar slices, "group" = split series,
            # "bucket" = table rows — all are terms-like grouping aggs
            group_aggs.append(agg)
        elif schema == "metric":
            metric_aggs.append(agg)

    # Build metric sub-aggs
    es_metric_aggs = {}
    for i, m in enumerate(metric_aggs):
        agg_type = m.get("type", "count")
        field = m.get("params", {}).get("field")
        agg_name = f"metric_{i}"
        if agg_type == "count":
            if field:
                es_metric_aggs[agg_name] = {"value_count": {"field": field}}
            # else: doc_count is implicit
        elif agg_type in ("avg", "sum", "min", "max", "median"):
            if field:
                es_type = "avg" if agg_type == "median" else agg_type
                es_metric_aggs[agg_name] = {es_type: {"field": field}}
        elif agg_type == "percentiles" and field:
            percents = m.get("params", {}).get("percents", [50, 95, 99])
            es_metric_aggs[agg_name] = {"percentiles": {"field": field, "percents": percents}}
        elif agg_type == "cardinality" and field:
            es_metric_aggs[agg_name] = {"cardinality": {"field": field}}

    # Build bucket agg chain: segment (date_histogram) > group (terms) > metrics
    def _wrap_in_terms_chain(terms_aggs, inner_aggs):
        current = inner_aggs
        for t in reversed(terms_aggs):
            field = t.get("params", {}).get("field")
            size = t.get("params", {}).get("size", 20)
            order_by = t.get("params", {}).get("orderBy", "_count")
            order_dir = t.get("params", {}).get("order", "desc")
            if field:
                order_key = order_by if order_by in ("_count", "_key") else "_count"
                current = {f"by_{field}": {"terms": {"field": field, "size": size, "order": {order_key: order_dir}}, "aggs": current}}
        return current

    inner = _wrap_in_terms_chain(group_aggs, es_metric_aggs)

    # Wrap in date_histogram if present
    top_aggs = inner
    for seg in segment_aggs:
        if seg.get("type") == "date_histogram":
            field = seg.get("params", {}).get("field", "timestamp")
            interval = (
                seg.get("params", {}).get("fixed_interval")
                or seg.get("params", {}).get("calendar_interval")
                or seg.get("params", {}).get("interval")
                or "1m"
            )
            if interval == "auto":
                interval = "1m"
            top_aggs = {"by_time": {"date_histogram": {"field": field, "fixed_interval": interval, "min_doc_count": 0}, "aggs": inner}}
            break

    return {"size": 0, "query": es_filter, "aggs": top_aggs}


def _build_es_query_from_lens_columns(
    columns: dict,
    filter_query: str | None,
) -> dict:
    """Build an ES query body from Lens column definitions.

    Maps Lens operationTypes to their ES aggregation equivalents, preserving
    sizes, ordering, and nesting order from the column definitions.
    """
    es_filter = (
        {"query_string": {"query": filter_query}} if filter_query
        else {"match_all": {}}
    )

    date_hist_cols = []
    terms_cols = []
    metric_cols = []

    for col_id, col in columns.items():
        op = col.get("operationType", "")
        if op == "date_histogram":
            date_hist_cols.append(col)
        elif op == "terms":
            terms_cols.append(col)
        elif op in ("count", "sum", "average", "min", "max", "median",
                     "percentile", "unique_count", "last_value",
                     "cardinality"):
            metric_cols.append(col)

    # Build metric sub-aggs
    es_metric_aggs = {}
    for i, col in enumerate(metric_cols):
        op = col.get("operationType", "")
        field = col.get("sourceField")
        agg_name = f"metric_{i}"
        type_map = {
            "average": "avg", "sum": "sum", "min": "min", "max": "max",
            "median": "median", "unique_count": "cardinality",
            "cardinality": "cardinality",
        }
        es_type = type_map.get(op)
        if es_type and field:
            es_metric_aggs[agg_name] = {es_type: {"field": field}}
        elif op == "percentile" and field:
            pct = col.get("params", {}).get("percentile", 95)
            es_metric_aggs[agg_name] = {"percentiles": {"field": field, "percents": [pct]}}
        elif op == "count":
            if field and field != "Records":
                es_metric_aggs[agg_name] = {"value_count": {"field": field}}

    # Build nested terms chain, innermost has metric aggs
    current = es_metric_aggs
    for col in reversed(terms_cols):
        field = col.get("sourceField")
        size = col.get("params", {}).get("size", 20)
        order_dir = col.get("params", {}).get("orderDirection", "desc")
        if field:
            current = {f"by_{field.replace('.', '_')}": {
                "terms": {"field": field, "size": size, "order": {"_count": order_dir}},
                "aggs": current,
            }}

    # Wrap in date_histogram if present
    if date_hist_cols:
        col = date_hist_cols[0]
        field = col.get("sourceField", "timestamp")
        interval = col.get("params", {}).get("interval", "1m")
        if interval == "auto":
            interval = "1m"
        current = {"by_time": {
            "date_histogram": {"field": field, "fixed_interval": interval, "min_doc_count": 0},
            "aggs": current,
        }}

    return {"size": 0, "query": es_filter, "aggs": current}


def _extract_index_and_time_field_from_refs(
    references: list[dict],
    conn: KibanaConnection | None = None,
) -> tuple[str | None, str | None]:
    """Find the ES index pattern and time field from a saved object's references.

    References contain a data view ID, not the actual ES index name.
    Resolve via the Kibana data views API, falling back to the raw ID.

    Returns (index_pattern, time_field_name) tuple.
    """
    for ref in references:
        if ref.get("type") == "index-pattern":
            data_view_id = ref.get("id")
            client, base_url = _get_client_and_url(conn)
            response = client.get(
                f"{base_url}/api/data_views/data_view/{data_view_id}",
            )
            if response.status_code == 200:
                dv = response.json().get("data_view", {})
                index = dv.get("title")
                time_field = dv.get("timeFieldName")
                return (index or data_view_id, time_field)
            return (data_view_id, None)
    return (None, None)


def _extract_index_from_refs(
    references: list[dict],
    conn: KibanaConnection | None = None,
) -> str | None:
    """Find the ES index pattern from a saved object's references."""
    index, _ = _extract_index_and_time_field_from_refs(references, conn=conn)
    return index


def _extract_query_string(search_source: dict) -> str | None:
    """Extract the KQL/Lucene query string from a searchSourceJSON dict."""
    query = search_source.get("query", {})
    query_str = query.get("query", "")
    if query_str and query_str.strip():
        return query_str.strip()
    return None


def _extract_filter_fields(search_source: dict) -> list[str]:
    """Extract field names from structured filters in a searchSourceJSON dict.

    Kibana filters come in several shapes:
    - {"meta": {"key": "field_name", ...}, "query": {...}}
    - {"range": {"field_name": {...}}}
    - {"match_phrase": {"field_name": "value"}}
    - {"exists": {"field": "field_name"}}

    Compound bool filters are skipped (too complex to reliably extract).
    Returns a deduplicated list of field names.
    """
    filters = search_source.get("filter", [])
    fields: list[str] = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        # Most common: meta.key
        meta_key = f.get("meta", {}).get("key")
        if meta_key and meta_key != "$state":
            fields.append(meta_key)
            continue
        # range filter: {"range": {"timestamp": {...}}}
        if "range" in f and isinstance(f["range"], dict):
            fields.extend(f["range"].keys())
            continue
        # match_phrase: {"match_phrase": {"field": "val"}}
        if "match_phrase" in f and isinstance(f["match_phrase"], dict):
            fields.extend(f["match_phrase"].keys())
            continue
        # exists: {"exists": {"field": "name"}}
        if "exists" in f and isinstance(f["exists"], dict):
            field_val = f["exists"].get("field")
            if field_val:
                fields.append(field_val)
            continue
        # Unrecognized filter shape — log and skip
        log.debug("Unrecognized filter shape in searchSourceJSON: %s", list(f.keys()))

    return list(dict.fromkeys(fields))  # deduplicate preserving order


# Known Kibana control panel types. These are filter UI elements, not
# aggregation panels — we extract their target field names to understand
# which fields users filter by on this dashboard.
_CONTROL_PANEL_TYPES = {
    "optionsListControl",
    "rangeSliderControl",
    "timeSlider",
}


def _extract_control_panel_fields(panels_json: list[dict]) -> list[str]:
    """Extract field names from dashboard control panels.

    Kibana 8.x control panels appear in panelsJSON with types like
    ``optionsListControl`` (standalone) or nested inside a ``controlGroup``
    parent. The field being filtered is in ``explicitInput.fieldName`` or
    ``embeddableConfig.fieldName``.

    Legacy ``input_control_vis`` panels store fields in their saved object's
    ``visState.params.controls[].fieldName`` — we extract from the
    ``embeddableConfig`` if available inline.
    """
    fields: list[str] = []
    for panel in panels_json:
        panel_type = panel.get("type", "")

        if panel_type in _CONTROL_PANEL_TYPES:
            field = (
                panel.get("explicitInput", {}).get("fieldName")
                or panel.get("embeddableConfig", {}).get("fieldName")
            )
            if field:
                fields.append(field)

        elif panel_type == "controlGroup":
            # Control groups contain child controls in embeddableConfig.panels
            children = panel.get("embeddableConfig", {}).get("panels", {})
            if isinstance(children, dict):
                children = children.values()
            for child in children:
                if not isinstance(child, dict):
                    continue
                field = (
                    child.get("explicitInput", {}).get("fieldName")
                    or child.get("embeddableConfig", {}).get("fieldName")
                )
                if field:
                    fields.append(field)

        elif panel_type == "input_control_vis":
            # Legacy input controls — field names in embeddableConfig inline
            controls = (
                panel.get("embeddableConfig", {})
                .get("vis", {})
                .get("params", {})
                .get("controls", [])
            )
            for ctrl in controls:
                field = ctrl.get("fieldName")
                if field:
                    fields.append(field)

        elif "control" in panel_type.lower() and panel_type not in _CONTROL_PANEL_TYPES:
            log.warning("Unrecognized control panel type: %s", panel_type)

    return list(dict.fromkeys(fields))  # deduplicate preserving order


# ── Metrics Dashboard (write operations) ─────────────────────────────

METRICS_DASHBOARD_ID = "l2m-metrics-dashboard"
METRICS_VIS_PREFIX = "l2m-metrics-vis-rule-"
METRICS_DV_PREFIX = "l2m-metrics-dv-rule-"

# Metric agg mapping: compute_type -> (agg_type, field_name_template)
_METRIC_AGG_MAP = {
    "count": ("sum", "event_count"),
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

    # 3. Build data view saved object (included in NDJSON import to avoid reference issues)
    dv_obj = {
        "id": dv_id,
        "type": "index-pattern",
        "attributes": {
            "title": index_pattern,
            "timeFieldName": time_field,
            "name": f"Metrics: {rule_name}",
        },
    }

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

    # 8. Import data view + visualization + updated dashboard
    return _import_saved_objects([dv_obj, vis_obj, dashboard_obj], conn=conn)


def remove_rule_panel_from_dashboard(
    rule_id: int,
    conn: KibanaConnection | None = None,
) -> dict:
    """Remove a rule's panel from the metrics dashboard.

    Strips the panel from panelsJSON, removes the reference, and re-imports
    the dashboard. Also deletes the visualization and data view saved objects.
    """
    dashboard = get_dashboard(METRICS_DASHBOARD_ID, conn=conn)
    attrs = dashboard["attributes"]
    existing_panels = json.loads(attrs.get("panelsJSON", "[]"))
    existing_refs = dashboard.get("references", [])

    panel_index = f"p_rule_{rule_id}"
    ref_name = f"panel_{panel_index}"

    updated_panels = [p for p in existing_panels if p.get("panelIndex") != panel_index]
    updated_refs = [r for r in existing_refs if r.get("name") != ref_name]

    # Re-import updated dashboard
    dashboard_obj = {
        "id": METRICS_DASHBOARD_ID,
        "type": "dashboard",
        "attributes": {
            **attrs,
            "panelsJSON": json.dumps(updated_panels),
        },
        "references": updated_refs,
    }
    result = _import_saved_objects([dashboard_obj], conn=conn)

    # Best-effort cleanup of the visualization and data view saved objects
    vis_id = f"{METRICS_VIS_PREFIX}{rule_id}"
    dv_id = f"{METRICS_DV_PREFIX}{rule_id}"
    _delete_saved_object("visualization", vis_id, conn=conn)
    _delete_saved_object("index-pattern", dv_id, conn=conn)

    return result


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


def _delete_saved_object(
    obj_type: str,
    obj_id: str,
    conn: KibanaConnection | None = None,
) -> None:
    """Delete a Kibana saved object. Ignores 404 (already gone)."""
    client, base_url = _get_client_and_url(conn)
    response = client.delete(f"{base_url}/api/saved_objects/{obj_type}/{obj_id}")
    if response.status_code not in (200, 404):
        log.warning("Failed to delete %s/%s: %s", obj_type, obj_id, response.status_code)


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
            "allowNoIndex": True,
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
    agg_type, field_template = _METRIC_AGG_MAP.get(compute_type, ("sum", "event_count"))
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

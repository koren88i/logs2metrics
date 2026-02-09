"""Dashboard analyzer — scores each panel's suitability for metric conversion.

Orchestrates the Kibana connector (panel parsing), ES connector (field types),
and the scoring engine to produce a full DashboardAnalysis.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

import es_connector
import kibana_connector
from connector_models import FieldMapping, PanelAnalysis
from kibana_connector import KibanaConnection
from scoring import SuitabilityScore, score_panel

log = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────


class PanelScore(BaseModel):
    panel: PanelAnalysis
    score: SuitabilityScore


class DashboardAnalysis(BaseModel):
    dashboard_id: str
    dashboard_title: str
    panels: list[PanelScore] = Field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────


def analyze_dashboard(
    dashboard_id: str,
    lookback_override: str | None = None,
    conn: KibanaConnection | None = None,
) -> DashboardAnalysis:
    """Fetch a dashboard, score every panel, return structured analysis.

    Args:
        dashboard_id: Kibana saved-object ID for the dashboard.
        lookback_override: If provided (e.g. ``"now-7d"``), overrides the
            dashboard's saved ``timeFrom`` for scoring purposes.  This lets
            the user tell us how they *actually* use the dashboard.
    """

    # Parsed panels
    detail = kibana_connector.get_dashboard_with_panels(dashboard_id, conn=conn)

    # Raw dashboard attributes for behavioral signals
    raw = kibana_connector.get_dashboard(dashboard_id, conn=conn)
    attrs = raw.get("attributes", {})

    # User-supplied lookback wins; otherwise fall back to dashboard metadata
    time_from = lookback_override or (
        attrs.get("timeFrom") if attrs.get("timeRestore") else None
    )

    refresh = attrs.get("refreshInterval", {})
    refresh_ms = (
        refresh.get("value")
        if isinstance(refresh, dict) and not refresh.get("pause", True)
        else None
    )

    # Resolve ES field types per index pattern (cached across panels)
    field_type_cache: dict[str, dict[str, FieldMapping]] = {}
    for panel in detail.panels:
        ip = panel.index_pattern
        if ip and ip not in field_type_cache:
            field_type_cache[ip] = _resolve_field_types(ip, conn=conn)

    # Score each panel
    scored: list[PanelScore] = []
    for panel in detail.panels:
        ft = field_type_cache.get(panel.index_pattern) if panel.index_pattern else None
        result = score_panel(
            panel=panel,
            field_types=ft,
            dashboard_time_from=time_from,
            refresh_interval_ms=refresh_ms,
        )
        scored.append(PanelScore(panel=panel, score=result))

    return DashboardAnalysis(
        dashboard_id=detail.id,
        dashboard_title=detail.title,
        panels=scored,
    )


# ── Helpers ───────────────────────────────────────────────────────────


def _resolve_field_types(data_view_id: str, conn: KibanaConnection | None = None) -> dict[str, FieldMapping]:
    """Resolve a Kibana data view ID to a dict of field_name → FieldMapping."""
    try:
        index_pattern = kibana_connector.get_data_view_index_pattern(data_view_id, conn=conn)
        if not index_pattern:
            index_pattern = data_view_id  # fallback: treat ID as pattern

        mapping = es_connector.get_mapping(index_pattern)
        return {f.name: f for f in mapping.fields}
    except Exception:
        log.warning(
            "Could not resolve field types for %s", data_view_id, exc_info=True
        )
        return {}

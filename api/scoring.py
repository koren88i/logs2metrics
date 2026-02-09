"""Suitability scoring engine for Kibana panel → metric conversion candidates.

Produces a deterministic score (0–95) with a human-readable breakdown
for each signal that contributes to the total.

Scoring signals (max 95):
  Structural (from panel shape):
    +25  Uses date_histogram aggregation
    +20  Only numeric aggregations (count/sum/avg/percentiles/…)
    +15  No raw docs / top_hits
    +10  Group-by fields are keyword / aggregatable
  Behavioral (from dashboard usage):
    +15  Lookback window ≥ 7 days
    +10  Auto-refresh enabled
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from connector_models import FieldMapping, PanelAnalysis

# Aggregation types that produce numeric metrics suitable for pre-aggregation
NUMERIC_AGG_TYPES = {
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "percentiles",
    "cardinality",
    "value_count",
    "median_absolute_deviation",
}


# ── Models ────────────────────────────────────────────────────────────


class ScoreBreakdown(BaseModel):
    signal: str
    points: int
    max_points: int
    explanation: str


class SuitabilityScore(BaseModel):
    total: int
    max_total: int
    breakdown: list[ScoreBreakdown] = Field(default_factory=list)
    recommendation_text: str


# ── Public API ────────────────────────────────────────────────────────


def score_panel(
    panel: PanelAnalysis,
    field_types: dict[str, FieldMapping] | None = None,
    dashboard_time_from: str | None = None,
    refresh_interval_ms: int | None = None,
) -> SuitabilityScore:
    """Score a parsed panel's suitability for metric conversion (0-95)."""
    breakdown: list[ScoreBreakdown] = []

    _score_date_histogram(panel, breakdown)
    _score_numeric_aggs(panel, breakdown)
    _score_no_raw_docs(panel, breakdown)
    _score_aggregatable_dimensions(panel, field_types, breakdown)
    _score_lookback(dashboard_time_from, breakdown)
    _score_auto_refresh(refresh_interval_ms, breakdown)

    total = sum(b.points for b in breakdown)
    max_total = sum(b.max_points for b in breakdown)
    recommendation = _generate_recommendation(total, panel)

    return SuitabilityScore(
        total=total,
        max_total=max_total,
        breakdown=breakdown,
        recommendation_text=recommendation,
    )


# ── Individual signal scorers ─────────────────────────────────────────


def _score_date_histogram(
    panel: PanelAnalysis, out: list[ScoreBreakdown]
) -> None:
    has = "date_histogram" in panel.agg_types
    out.append(
        ScoreBreakdown(
            signal="date_histogram",
            points=25 if has else 0,
            max_points=25,
            explanation=(
                "Panel uses date_histogram aggregation — ideal for "
                "time-bucketed metrics."
                if has
                else "Panel does not use date_histogram — time-series "
                "bucketing not detected."
            ),
        )
    )


def _score_numeric_aggs(
    panel: PanelAnalysis, out: list[ScoreBreakdown]
) -> None:
    if panel.metrics:
        all_numeric = all(m.type in NUMERIC_AGG_TYPES for m in panel.metrics)
        if all_numeric:
            agg_list = ", ".join(m.type for m in panel.metrics)
            out.append(
                ScoreBreakdown(
                    signal="numeric_aggs",
                    points=20,
                    max_points=20,
                    explanation=f"All metrics are numeric aggregations ({agg_list}).",
                )
            )
        else:
            non_num = [
                m.type
                for m in panel.metrics
                if m.type not in NUMERIC_AGG_TYPES
            ]
            out.append(
                ScoreBreakdown(
                    signal="numeric_aggs",
                    points=0,
                    max_points=20,
                    explanation="Non-numeric aggregations detected: "
                    f"{', '.join(non_num)}.",
                )
            )
    elif not panel.has_raw_docs:
        out.append(
            ScoreBreakdown(
                signal="numeric_aggs",
                points=10,
                max_points=20,
                explanation="No explicit metric aggregations found; "
                "may default to count.",
            )
        )
    else:
        out.append(
            ScoreBreakdown(
                signal="numeric_aggs",
                points=0,
                max_points=20,
                explanation="Panel shows raw documents — no numeric "
                "aggregations.",
            )
        )


def _score_no_raw_docs(
    panel: PanelAnalysis, out: list[ScoreBreakdown]
) -> None:
    out.append(
        ScoreBreakdown(
            signal="no_raw_docs",
            points=0 if panel.has_raw_docs else 15,
            max_points=15,
            explanation=(
                "Panel does not display raw log lines."
                if not panel.has_raw_docs
                else "Panel displays raw documents — cannot be converted "
                "to metrics."
            ),
        )
    )


def _score_aggregatable_dimensions(
    panel: PanelAnalysis,
    field_types: dict[str, FieldMapping] | None,
    out: list[ScoreBreakdown],
) -> None:
    if panel.group_by_fields and field_types:
        all_agg = all(
            f in field_types and field_types[f].aggregatable
            for f in panel.group_by_fields
        )
        if all_agg:
            dims = ", ".join(panel.group_by_fields)
            out.append(
                ScoreBreakdown(
                    signal="aggregatable_dimensions",
                    points=10,
                    max_points=10,
                    explanation="All group-by fields are aggregatable: "
                    f"{dims}.",
                )
            )
        else:
            non_agg = [
                f
                for f in panel.group_by_fields
                if f not in field_types or not field_types[f].aggregatable
            ]
            out.append(
                ScoreBreakdown(
                    signal="aggregatable_dimensions",
                    points=0,
                    max_points=10,
                    explanation="Non-aggregatable group-by fields: "
                    f"{', '.join(non_agg)}.",
                )
            )
    elif panel.group_by_fields:
        dims = ", ".join(panel.group_by_fields)
        out.append(
            ScoreBreakdown(
                signal="aggregatable_dimensions",
                points=5,
                max_points=10,
                explanation=f"Group-by fields present ({dims}) but field "
                "types not verified.",
            )
        )
    else:
        out.append(
            ScoreBreakdown(
                signal="aggregatable_dimensions",
                points=5,
                max_points=10,
                explanation="No group-by dimensions — metric would be a "
                "simple time series.",
            )
        )


def _score_lookback(
    dashboard_time_from: str | None, out: list[ScoreBreakdown]
) -> None:
    if not dashboard_time_from:
        out.append(
            ScoreBreakdown(
                signal="lookback_window",
                points=0,
                max_points=15,
                explanation="No dashboard lookback information available.",
            )
        )
        return

    days = _parse_lookback_days(dashboard_time_from)
    if days is None:
        out.append(
            ScoreBreakdown(
                signal="lookback_window",
                points=0,
                max_points=15,
                explanation="Could not parse dashboard lookback period.",
            )
        )
    elif days >= 7:
        out.append(
            ScoreBreakdown(
                signal="lookback_window",
                points=15,
                max_points=15,
                explanation=f"Dashboard lookback is ~{days} days — long "
                "lookback benefits most from pre-aggregation.",
            )
        )
    else:
        out.append(
            ScoreBreakdown(
                signal="lookback_window",
                points=5,
                max_points=15,
                explanation=f"Dashboard lookback is ~{days} days — shorter "
                "windows benefit less from pre-aggregation.",
            )
        )


def _score_auto_refresh(
    refresh_interval_ms: int | None, out: list[ScoreBreakdown]
) -> None:
    if refresh_interval_ms is not None and refresh_interval_ms > 0:
        secs = refresh_interval_ms // 1000
        out.append(
            ScoreBreakdown(
                signal="auto_refresh",
                points=10,
                max_points=10,
                explanation=f"Auto-refresh enabled (every {secs}s) — "
                "repeated queries benefit from pre-aggregation.",
            )
        )
    else:
        out.append(
            ScoreBreakdown(
                signal="auto_refresh",
                points=0,
                max_points=10,
                explanation="Auto-refresh not enabled or not detected.",
            )
        )


# ── Helpers ───────────────────────────────────────────────────────────


def _parse_lookback_days(time_from: str) -> int | None:
    """Parse Kibana relative time like ``now-7d`` into approximate days."""
    if not time_from.startswith("now-"):
        return None
    suffix = time_from[4:]
    try:
        if suffix.endswith("d"):
            return int(suffix[:-1])
        if suffix.endswith("w"):
            return int(suffix[:-1]) * 7
        if suffix.endswith("M"):
            return int(suffix[:-1]) * 30
        if suffix.endswith("y"):
            return int(suffix[:-1]) * 365
        if suffix.endswith("h"):
            return max(1, int(suffix[:-1]) // 24)
        if suffix.endswith("m"):
            return max(1, int(suffix[:-1]) // 1440)
    except (ValueError, IndexError):
        return None
    return None


def _generate_recommendation(total: int, panel: PanelAnalysis) -> str:
    """Generate human-readable recommendation text based on score."""
    if panel.has_raw_docs:
        return (
            "This panel displays raw log lines and cannot be converted "
            "to a metric. Consider creating a separate aggregation-based "
            "visualization if metrics are needed."
        )
    if total >= 70:
        return (
            f"Strong candidate for metric conversion (score: {total}). "
            "This panel's aggregations can be efficiently pre-computed "
            "as a metric, reducing query cost and improving dashboard "
            "performance."
        )
    if total >= 40:
        return (
            f"Moderate candidate for metric conversion (score: {total}). "
            "This panel could benefit from pre-aggregation, but review "
            "the scoring breakdown to understand potential limitations."
        )
    return (
        f"Weak candidate for metric conversion (score: {total}). "
        "This panel may not benefit significantly from conversion to "
        "metrics. Review the breakdown for details."
    )

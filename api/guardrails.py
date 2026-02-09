"""Pre-creation guardrails for log-to-metric rules.

Validates that a proposed rule won't create excessive cardinality,
uses too many dimensions, or cost more than the logs it replaces.
Inspired by Datadog's cardinality warnings for log-based metrics.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cost_estimator import (
    HIGH_CARDINALITY_FIELDS,
    CostEstimate,
    estimate_cost,
)
from models import RuleCreate
import es_connector

# ── Thresholds ───────────────────────────────────────────────────────

MAX_SERIES_COUNT = 100_000
MAX_DIMENSIONS = 5
HIGH_CARDINALITY_THRESHOLD = 10_000  # per-field cardinality warning


# ── Models ───────────────────────────────────────────────────────────


class GuardrailResult(BaseModel):
    name: str
    passed: bool
    explanation: str
    suggested_fix: str | None = None


class GuardrailsReport(BaseModel):
    all_passed: bool
    results: list[GuardrailResult] = Field(default_factory=list)
    cost_estimate: CostEstimate


class EstimateResponse(BaseModel):
    cost_estimate: CostEstimate
    guardrails: list[GuardrailResult] = Field(default_factory=list)
    all_guardrails_passed: bool


# ── Public API ───────────────────────────────────────────────────────


def evaluate(rule: RuleCreate) -> GuardrailsReport:
    """Run all guardrails against a proposed rule and return the report."""
    cost = estimate_cost(rule)
    results: list[GuardrailResult] = []

    _check_dimension_limit(rule, results)
    _check_cardinality(rule, cost, results)
    _check_high_cardinality_fields(rule, results)
    _check_net_savings(cost, results)

    return GuardrailsReport(
        all_passed=all(r.passed for r in results),
        results=results,
        cost_estimate=cost,
    )


# ── Individual guardrail checks ─────────────────────────────────────


def _check_dimension_limit(
    rule: RuleCreate, out: list[GuardrailResult]
) -> None:
    """Enforce maximum number of group-by dimensions."""
    dims = rule.group_by.dimensions
    count = len(dims)

    if count <= MAX_DIMENSIONS:
        out.append(GuardrailResult(
            name="dimension_limit",
            passed=True,
            explanation=(
                f"Rule uses {count} dimension(s) "
                f"(limit: {MAX_DIMENSIONS})."
            ),
        ))
    else:
        out.append(GuardrailResult(
            name="dimension_limit",
            passed=False,
            explanation=(
                f"Rule uses {count} dimensions, exceeding the limit "
                f"of {MAX_DIMENSIONS}. More dimensions = exponentially "
                "more metric series."
            ),
            suggested_fix=(
                f"Reduce to at most {MAX_DIMENSIONS} dimensions. "
                "Remove the least important group-by fields: "
                f"{', '.join(dims[MAX_DIMENSIONS:])}."
            ),
        ))


def _check_cardinality(
    rule: RuleCreate,
    cost: CostEstimate,
    out: list[GuardrailResult],
) -> None:
    """Enforce maximum estimated series count."""
    series = cost.estimated_series_count

    if series <= MAX_SERIES_COUNT:
        out.append(GuardrailResult(
            name="cardinality",
            passed=True,
            explanation=(
                f"Estimated series count: {series:,} "
                f"(limit: {MAX_SERIES_COUNT:,})."
            ),
        ))
    else:
        out.append(GuardrailResult(
            name="cardinality",
            passed=False,
            explanation=(
                f"Estimated series count is {series:,}, exceeding the "
                f"limit of {MAX_SERIES_COUNT:,}. This would create "
                "excessive metric data."
            ),
            suggested_fix=(
                "Remove high-cardinality dimensions or add a filter "
                "to reduce the number of unique dimension combinations. "
                "Avoid grouping by unbounded attributes like user_id, "
                "request_id, or session_id."
            ),
        ))


def _check_high_cardinality_fields(
    rule: RuleCreate, out: list[GuardrailResult]
) -> None:
    """Warn if any dimension is a known high-cardinality field name."""
    dims = rule.group_by.dimensions
    flagged = [d for d in dims if d.lower() in HIGH_CARDINALITY_FIELDS]

    if not flagged:
        out.append(GuardrailResult(
            name="high_cardinality_fields",
            passed=True,
            explanation="No known high-cardinality field names detected.",
        ))
    else:
        out.append(GuardrailResult(
            name="high_cardinality_fields",
            passed=False,
            explanation=(
                f"Dimension(s) {', '.join(flagged)} are typically "
                "unbounded high-cardinality fields. Grouping by these "
                "will produce an excessive number of metric series."
            ),
            suggested_fix=(
                f"Remove {', '.join(flagged)} from group-by dimensions. "
                "Use these fields in filters instead if needed."
            ),
        ))


def _check_net_savings(
    cost: CostEstimate, out: list[GuardrailResult]
) -> None:
    """Enforce that metric storage is less than log storage (net savings > 0)."""
    if cost.savings_gb > 0:
        out.append(GuardrailResult(
            name="net_savings",
            passed=True,
            explanation=(
                f"Estimated savings: {cost.savings_gb:.2f} GB "
                f"({cost.savings_pct:.1f}%). Metric storage "
                f"({cost.metric_storage_gb:.4f} GB) is less than "
                f"log storage ({cost.log_storage_gb:.4f} GB)."
            ),
        ))
    else:
        out.append(GuardrailResult(
            name="net_savings",
            passed=False,
            explanation=(
                f"Metric storage ({cost.metric_storage_gb:.4f} GB) "
                f"would exceed log storage ({cost.log_storage_gb:.4f} GB). "
                "This conversion would increase costs."
            ),
            suggested_fix=(
                "Use a larger time bucket (e.g. '5m' instead of '1m') "
                "or reduce the number of dimensions to decrease the "
                "metric series count."
            ),
        ))

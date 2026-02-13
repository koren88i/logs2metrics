"""Logs2Metrics API — LogMetricRule CRUD service."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any
from pathlib import Path

from elasticsearch import Elasticsearch, NotFoundError as ESNotFoundError
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from httpx import HTTPStatusError
import httpx
from sqlmodel import Session, select

import analyzer
import es_connector
import guardrails
import kibana_connector
from analyzer import DashboardAnalysis
from kibana_connector import KibanaConnection
from backend import BackendStatus, TransformHealth
from connector_models import (
    DashboardDetail,
    DashboardSummary,
    FieldCardinality,
    IndexInfo,
    IndexMapping,
    IndexStats,
)
from config import ES_URL, HEALTH_CHECK_INTERVAL, KIBANA_URL
from database import create_db, get_session
from elastic_backend import backend as metrics_backend
from guardrails import EstimateResponse, GuardrailResult
from models import (
    ComputeConfig,
    GroupByConfig,
    LogMetricRule,
    OriginConfig,
    RuleCreate,
    RuleResponse,
    RuleStatus,
    RuleUpdate,
    SourceConfig,
)
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

app = FastAPI(title="Logs2Metrics API", version="0.6.0")


# ── Health monitor state ──────────────────────────────────────────────
_health_monitor_state: dict[str, Any] = {
    "last_check_time": None,
    "rules_in_error": [],
    "running": False,
}


@app.exception_handler(ESNotFoundError)
async def es_not_found_handler(request, exc):
    return JSONResponse(
        status_code=404,
        content={"detail": f"Elasticsearch resource not found: {exc}"},
    )


@app.exception_handler(HTTPStatusError)
async def httpx_error_handler(request, exc):
    url = str(exc.request.url)
    status = exc.response.status_code
    if status == 404:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Upstream resource not found: {url}"},
        )
    return JSONResponse(
        status_code=502,
        content={"detail": f"Upstream error {status}: {url}"},
    )


@app.on_event("startup")
async def on_startup():
    create_db()
    asyncio.create_task(_health_monitor_loop())


# ── Health monitor background task ────────────────────────────────────


async def _health_monitor_loop():
    """Background task: periodically check transform health for all active rules."""
    _health_monitor_state["running"] = True
    log.info("Health monitor started (interval=%ds)", HEALTH_CHECK_INTERVAL)

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            _check_all_active_rules()
        except asyncio.CancelledError:
            log.info("Health monitor cancelled")
            break
        except Exception:
            log.exception("Health monitor error (will retry next cycle)")


def _check_all_active_rules():
    """Check every active rule's transform health. Update DB if unhealthy."""
    from database import engine  # local import to avoid circular dependency at module level

    errors_found: list[dict] = []

    with Session(engine) as session:
        active_rules = session.exec(
            select(LogMetricRule).where(LogMetricRule.status == RuleStatus.active.value)
        ).all()

        for rule in active_rules:
            try:
                status = metrics_backend.get_status(rule.id)

                if status.health in (TransformHealth.red, TransformHealth.stopped):
                    rule.status = RuleStatus.error.value
                    rule.updated_at = datetime.utcnow()
                    session.add(rule)
                    log.warning(
                        "Health monitor: rule %d transform health=%s — setting status to error. Error: %s",
                        rule.id, status.health.value, status.error or "none",
                    )
                    errors_found.append({
                        "rule_id": rule.id,
                        "rule_name": rule.name,
                        "transform_health": status.health.value,
                        "error": status.error,
                    })
            except Exception:
                log.exception("Health monitor: failed to check rule %d", rule.id)

        if errors_found:
            session.commit()

    _health_monitor_state["last_check_time"] = datetime.utcnow().isoformat()
    _health_monitor_state["rules_in_error"] = errors_found
    if not errors_found:
        log.debug("Health monitor: all %d active rules healthy", len(active_rules))


# ── Kibana connection dependency ──────────────────────────────────────


def get_kibana_conn(
    x_kibana_url: str | None = Header(default=None),
    x_kibana_user: str | None = Header(default=None),
    x_kibana_pass: str | None = Header(default=None),
) -> KibanaConnection | None:
    """Extract optional Kibana connection override from request headers."""
    if not x_kibana_url:
        return None
    url = x_kibana_url.rstrip("/")
    username = x_kibana_user
    password = x_kibana_pass
    # Auto-fill auth from service map for known Kibana instances
    if not username or not password:
        svc = _KIBANA_SERVICE_MAP.get(url)
        if svc and "kibana_auth" in svc:
            username, password = svc["kibana_auth"]
    return KibanaConnection(url=url, username=username, password=password)


# ── CRUD endpoints ────────────────────────────────────────────────────


@app.post("/api/rules", response_model=RuleResponse, status_code=201)
def create_rule(body: RuleCreate, session: Session = Depends(get_session), skip_guardrails: bool = False):
    # Run guardrails before accepting the rule
    report = guardrails.evaluate(body)
    if not report.all_passed and not skip_guardrails:
        failures = [r for r in report.results if not r.passed]
        detail = [
            {
                "guardrail": f.name,
                "explanation": f.explanation,
                "suggested_fix": f.suggested_fix,
            }
            for f in failures
        ]
        raise HTTPException(
            status_code=422,
            detail={"message": "Guardrail validation failed", "failures": detail},
        )

    now = datetime.utcnow()
    rule = LogMetricRule(
        name=body.name,
        owner=body.owner,
        source=body.source.model_dump(),
        group_by=body.group_by.model_dump(),
        compute=body.compute.model_dump(),
        backend_config=body.backend_config.model_dump(),
        origin=body.origin.model_dump() if body.origin else {},
        status=body.status.value,
        created_at=now,
        updated_at=now,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)

    # Provision backend resources if rule is active
    if body.status == RuleStatus.active:
        result = metrics_backend.provision(rule)
        if not result.success:
            rule.status = RuleStatus.error.value
            rule.updated_at = datetime.utcnow()
            session.add(rule)
            session.commit()
            session.refresh(rule)

    return RuleResponse.from_db(rule)


@app.get("/api/rules", response_model=list[RuleResponse])
def list_rules(session: Session = Depends(get_session)):
    rules = session.exec(select(LogMetricRule)).all()
    return [RuleResponse.from_db(r) for r in rules]


@app.get("/api/rules/{rule_id}", response_model=RuleResponse)
def get_rule(rule_id: int, session: Session = Depends(get_session)):
    rule = session.get(LogMetricRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return RuleResponse.from_db(rule)


@app.put("/api/rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    rule_id: int, body: RuleUpdate, session: Session = Depends(get_session)
):
    rule = session.get(LogMetricRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    old_status = rule.status
    update_data = body.model_dump(exclude_unset=True)

    # Serialize nested Pydantic models to dicts for JSON columns
    for key in ("source", "group_by", "compute", "backend_config", "origin"):
        if key in update_data and update_data[key] is not None:
            update_data[key] = update_data[key].model_dump() if hasattr(update_data[key], "model_dump") else update_data[key]

    # Convert status enum to string
    if "status" in update_data and update_data["status"] is not None:
        update_data["status"] = update_data["status"].value if hasattr(update_data["status"], "value") else update_data["status"]

    for field, value in update_data.items():
        setattr(rule, field, value)

    rule.updated_at = datetime.utcnow()
    session.add(rule)
    session.commit()
    session.refresh(rule)

    # Handle status transitions
    new_status = rule.status
    if old_status != new_status:
        if new_status == RuleStatus.active.value and old_status != RuleStatus.active.value:
            result = metrics_backend.provision(rule)
            if not result.success:
                rule.status = RuleStatus.error.value
                rule.updated_at = datetime.utcnow()
                session.add(rule)
                session.commit()
                session.refresh(rule)
        elif old_status == RuleStatus.active.value and new_status != RuleStatus.active.value:
            try:
                metrics_backend.deprovision(rule.id)
            except Exception as e:
                log.warning("Deprovision error for rule %s: %s", rule_id, e)
    elif old_status == RuleStatus.active.value and new_status == RuleStatus.active.value:
        # Config changed on an active rule — reprovision if transform-affecting fields changed.
        # sync_delay, time_bucket, frequency, compute, and source are baked into the transform.
        config_fields = {"group_by", "compute", "source"}
        if config_fields & update_data.keys():
            log.info("Config changed on active rule %s — reprovisioning transform", rule_id)
            try:
                metrics_backend.deprovision(rule.id)
            except Exception as e:
                log.warning("Deprovision error during reprovision for rule %s: %s", rule_id, e)
            result = metrics_backend.provision(rule)
            if not result.success:
                rule.status = RuleStatus.error.value
                rule.updated_at = datetime.utcnow()
                session.add(rule)
                session.commit()
                session.refresh(rule)

    return RuleResponse.from_db(rule)


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, session: Session = Depends(get_session)):
    rule = session.get(LogMetricRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    # Deprovision backend resources if rule was active or errored
    if rule.status in (RuleStatus.active.value, RuleStatus.error.value):
        try:
            metrics_backend.deprovision(rule.id)
        except Exception as e:
            log.warning("Deprovision error for rule %s: %s", rule_id, e)

    session.delete(rule)
    session.commit()
    return None


# ── Backend status ────────────────────────────────────────────────────


@app.get("/api/rules/{rule_id}/status", response_model=BackendStatus)
def get_rule_backend_status(rule_id: int, session: Session = Depends(get_session)):
    """Return backend health and processing stats for a provisioned rule."""
    rule = session.get(LogMetricRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status not in (RuleStatus.active.value, RuleStatus.error.value):
        raise HTTPException(
            status_code=400,
            detail=f"Rule is in '{rule.status}' status — no backend resources to check",
        )
    return metrics_backend.get_status(rule_id)


# ── Health ────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/health")
def api_health():
    """Return health monitor status: last check time and any rules in error."""
    return {
        "status": "ok",
        "monitor_running": _health_monitor_state["running"],
        "last_check_time": _health_monitor_state["last_check_time"],
        "check_interval_seconds": HEALTH_CHECK_INTERVAL,
        "rules_in_error": _health_monitor_state["rules_in_error"],
    }


# -- ES Connector endpoints --------------------------------------------


@app.get("/api/es/indices", response_model=list[IndexInfo])
def api_list_indices(pattern: str = Query(default="*")):
    return es_connector.list_indices(pattern)


@app.get("/api/es/indices/{index}/mapping", response_model=IndexMapping)
def api_get_mapping(index: str):
    return es_connector.get_mapping(index)


@app.get("/api/es/indices/{index}/cardinality/{field}", response_model=FieldCardinality)
def api_get_field_cardinality(index: str, field: str):
    return es_connector.get_field_cardinality(index, field)


@app.get("/api/es/indices/{index}/stats", response_model=IndexStats)
def api_get_index_stats(index: str):
    return es_connector.get_index_stats(index)


# -- Kibana Connector endpoints ----------------------------------------


@app.get("/api/kibana/dashboards", response_model=list[DashboardSummary])
def api_list_dashboards(conn: KibanaConnection | None = Depends(get_kibana_conn)):
    return kibana_connector.list_dashboards(conn=conn)


@app.get("/api/kibana/dashboards/{dashboard_id}", response_model=DashboardDetail)
def api_get_dashboard(dashboard_id: str, conn: KibanaConnection | None = Depends(get_kibana_conn)):
    return kibana_connector.get_dashboard_with_panels(dashboard_id, conn=conn)


@app.get("/api/kibana/test-connection")
def api_test_kibana_connection(conn: KibanaConnection | None = Depends(get_kibana_conn)):
    """Quick health check against the configured (or overridden) Kibana instance."""
    url = conn.url if conn else KIBANA_URL
    try:
        auth = None
        if conn and conn.username and conn.password:
            auth = httpx.BasicAuth(conn.username, conn.password)
        with httpx.Client(headers={"kbn-xsrf": "true"}, follow_redirects=True, auth=auth, timeout=5) as client:
            resp = client.get(f"{url}/api/status")
            resp.raise_for_status()
            data = resp.json()
            return {
                "ok": True,
                "url": url,
                "kibana_version": data.get("version", {}).get("number", "unknown"),
                "status": data.get("status", {}).get("overall", {}).get("level", "unknown"),
            }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach Kibana at {url}: {e}")


# -- Server config endpoint ────────────────────────────────────────────


@app.get("/api/config")
def api_config():
    """Return server-side config (default URLs) so the UI can display them."""
    return {"kibana_url": KIBANA_URL}


# -- Metrics Dashboard endpoints ----------------------------------------


class CreateMetricsDashboardRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class MetricsDashboardResponse(BaseModel):
    dashboard_id: str
    title: str
    kibana_url: str
    panel_count: int = 0
    panels: list[dict] = Field(default_factory=list)


class AddPanelResponse(BaseModel):
    success: bool
    dashboard_id: str
    rule_id: int
    visualization_id: str
    data_view_id: str
    panel_count: int


@app.post("/api/metrics-dashboard", response_model=MetricsDashboardResponse, status_code=201)
def api_create_metrics_dashboard(
    body: CreateMetricsDashboardRequest,
    conn: KibanaConnection | None = Depends(get_kibana_conn),
):
    """Create an empty Kibana metrics dashboard."""
    result = kibana_connector.create_metrics_dashboard(body.title, conn=conn)
    if not result.get("success"):
        errors = result.get("errors", [])
        if errors:
            raise HTTPException(status_code=502, detail=f"Kibana import failed: {errors}")
    kibana_url = conn.url if conn else KIBANA_URL
    return MetricsDashboardResponse(
        dashboard_id=kibana_connector.METRICS_DASHBOARD_ID,
        title=body.title,
        kibana_url=f"{kibana_url}/app/dashboards#/view/{kibana_connector.METRICS_DASHBOARD_ID}",
        panel_count=0,
    )


@app.delete("/api/metrics-dashboard", status_code=200)
def api_delete_metrics_dashboard(
    conn: KibanaConnection | None = Depends(get_kibana_conn),
):
    """Delete the metrics dashboard and all its visualization/data-view saved objects."""
    dashboard = kibana_connector.get_metrics_dashboard(conn=conn)
    if not dashboard:
        raise HTTPException(status_code=404, detail="No metrics dashboard exists")

    # Extract rule IDs from panel indices to clean up saved objects
    panels = json.loads(dashboard["attributes"].get("panelsJSON", "[]"))
    for panel in panels:
        pi = panel.get("panelIndex", "")
        if pi.startswith("p_rule_"):
            try:
                rid = int(pi.replace("p_rule_", ""))
                kibana_connector._delete_saved_object(
                    "visualization", f"{kibana_connector.METRICS_VIS_PREFIX}{rid}", conn=conn
                )
                kibana_connector._delete_saved_object(
                    "index-pattern", f"{kibana_connector.METRICS_DV_PREFIX}{rid}", conn=conn
                )
            except (ValueError, TypeError):
                pass

    kibana_connector._delete_saved_object("dashboard", kibana_connector.METRICS_DASHBOARD_ID, conn=conn)
    return {"success": True}


@app.get("/api/metrics-dashboard", response_model=MetricsDashboardResponse)
def api_get_metrics_dashboard(
    conn: KibanaConnection | None = Depends(get_kibana_conn),
):
    """Get the current metrics dashboard info (if it exists)."""
    dashboard = kibana_connector.get_metrics_dashboard(conn=conn)
    if not dashboard:
        raise HTTPException(status_code=404, detail="No metrics dashboard exists yet")
    attrs = dashboard["attributes"]
    panels = json.loads(attrs.get("panelsJSON", "[]"))
    kibana_url = conn.url if conn else KIBANA_URL
    return MetricsDashboardResponse(
        dashboard_id=dashboard["id"],
        title=attrs.get("title", ""),
        kibana_url=f"{kibana_url}/app/dashboards#/view/{dashboard['id']}",
        panel_count=len(panels),
        panels=panels,
    )


@app.post("/api/metrics-dashboard/panels/{rule_id}", response_model=AddPanelResponse)
def api_add_panel_to_dashboard(
    rule_id: int,
    session: Session = Depends(get_session),
    conn: KibanaConnection | None = Depends(get_kibana_conn),
):
    """Add a rule's metrics visualization as a panel to the metrics dashboard."""
    rule = session.get(LogMetricRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.status != RuleStatus.active.value:
        raise HTTPException(
            status_code=400,
            detail=f"Rule must be active (current: {rule.status})",
        )

    # Verify metrics dashboard exists
    dashboard = kibana_connector.get_metrics_dashboard(conn=conn)
    if not dashboard:
        raise HTTPException(status_code=404, detail="No metrics dashboard exists. Create one first.")

    # Check if panel already exists
    existing_panels = json.loads(dashboard["attributes"].get("panelsJSON", "[]"))
    panel_index = f"p_rule_{rule_id}"
    if any(p.get("panelIndex") == panel_index for p in existing_panels):
        raise HTTPException(status_code=409, detail=f"Panel for rule #{rule_id} already exists in dashboard")

    # Extract rule config
    compute = ComputeConfig(**rule.compute)
    group_by = GroupByConfig(**rule.group_by)
    source = SourceConfig(**rule.source)
    origin = OriginConfig(**rule.origin) if rule.origin else None

    if not origin or not origin.panel_id:
        raise HTTPException(status_code=400, detail="Rule has no origin panel to clone from")

    try:
        result = kibana_connector.add_rule_panel_to_dashboard(
            rule_id=rule_id,
            rule_name=rule.name,
            origin_dashboard_id=origin.dashboard_id,
            origin_panel_id=origin.panel_id,
            compute_type=compute.type.value if hasattr(compute.type, "value") else compute.type,
            compute_field=compute.field,
            dimensions=group_by.dimensions,
            time_field=source.time_field,
            conn=conn,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    errors = result.get("errors", [])
    if errors:
        raise HTTPException(status_code=502, detail=f"Kibana import failed: {errors}")

    # Re-fetch to get updated panel count
    updated = kibana_connector.get_metrics_dashboard(conn=conn)
    updated_panels = json.loads(updated["attributes"].get("panelsJSON", "[]")) if updated else []

    return AddPanelResponse(
        success=True,
        dashboard_id=kibana_connector.METRICS_DASHBOARD_ID,
        rule_id=rule_id,
        visualization_id=f"{kibana_connector.METRICS_VIS_PREFIX}{rule_id}",
        data_view_id=f"{kibana_connector.METRICS_DV_PREFIX}{rule_id}",
        panel_count=len(updated_panels),
    )


@app.delete("/api/metrics-dashboard/panels/{rule_id}", status_code=200)
def api_remove_panel_from_dashboard(
    rule_id: int,
    conn: KibanaConnection | None = Depends(get_kibana_conn),
):
    """Remove a rule's panel from the metrics dashboard."""
    dashboard = kibana_connector.get_metrics_dashboard(conn=conn)
    if not dashboard:
        raise HTTPException(status_code=404, detail="No metrics dashboard exists")

    existing_panels = json.loads(dashboard["attributes"].get("panelsJSON", "[]"))
    panel_index = f"p_rule_{rule_id}"
    if not any(p.get("panelIndex") == panel_index for p in existing_panels):
        raise HTTPException(status_code=404, detail=f"Panel for rule #{rule_id} not found in dashboard")

    result = kibana_connector.remove_rule_panel_from_dashboard(rule_id, conn=conn)
    errors = result.get("errors", [])
    if errors:
        raise HTTPException(status_code=502, detail=f"Kibana import failed: {errors}")

    updated = kibana_connector.get_metrics_dashboard(conn=conn)
    updated_panels = json.loads(updated["attributes"].get("panelsJSON", "[]")) if updated else []
    return {"success": True, "rule_id": rule_id, "panel_count": len(updated_panels)}


# -- Analysis endpoints ------------------------------------------------


@app.post("/api/analyze/dashboard/{dashboard_id}", response_model=DashboardAnalysis)
def api_analyze_dashboard(
    dashboard_id: str,
    lookback: str | None = Query(default=None, description="Override lookback window, e.g. 'now-7d', 'now-30d'"),
    conn: KibanaConnection | None = Depends(get_kibana_conn),
):
    return analyzer.analyze_dashboard(dashboard_id, lookback_override=lookback, conn=conn)


# -- Cost Estimation + Guardrails endpoints ----------------------------


@app.post("/api/estimate", response_model=EstimateResponse)
def api_estimate(body: RuleCreate):
    """Estimate cost savings and validate guardrails for a draft rule."""
    report = guardrails.evaluate(body)
    return EstimateResponse(
        cost_estimate=report.cost_estimate,
        guardrails=report.results,
        all_guardrails_passed=report.all_passed,
    )


# -- Debug UI + proxy endpoints ──────────────────────────────────────


_es = Elasticsearch(ES_URL)

# Map Kibana URLs to their corresponding log-generator and ES service URLs
_KIBANA_SERVICE_MAP = {
    "http://kibana:5601": {
        "log_generator": "http://log-generator:8000",
        "es_url": "http://elasticsearch:9200",
    },
    "http://kibana2:5601": {
        "log_generator": "http://log-generator2:8000",
        "es_url": "http://elasticsearch2:9200",
        "es_auth": ("elastic", "admin1"),
        "kibana_auth": ("elastic", "admin1"),
    },
}


def _get_log_generator_url(x_kibana_url: str | None) -> str:
    """Resolve the log-generator URL from the connected Kibana URL."""
    if x_kibana_url:
        svc = _KIBANA_SERVICE_MAP.get(x_kibana_url.rstrip("/"))
        if svc:
            return svc["log_generator"]
    return "http://log-generator:8000"


def _get_es_client(x_kibana_url: str | None) -> Elasticsearch:
    """Resolve the ES client from the connected Kibana URL."""
    if x_kibana_url:
        svc = _KIBANA_SERVICE_MAP.get(x_kibana_url.rstrip("/"))
        if svc:
            kwargs = {}
            if "es_auth" in svc:
                kwargs["basic_auth"] = svc["es_auth"]
            return Elasticsearch(svc["es_url"], **kwargs)
    return _es


@app.post("/api/debug/generate")
def debug_generate(request_body: dict, x_kibana_url: str | None = Header(default=None)):
    """Proxy to log-generator service to avoid CORS."""
    count = request_body.get("count", 10)
    gen_url = _get_log_generator_url(x_kibana_url)
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{gen_url}/generate",
            json={"count": count},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/debug/generate-recent")
def debug_generate_recent(request_body: dict, x_kibana_url: str | None = Header(default=None)):
    """Proxy to log-generator recent logs (timestamped at now) for live injection."""
    count = request_body.get("count", 50)
    gen_url = _get_log_generator_url(x_kibana_url)
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{gen_url}/generate-recent",
            json={"count": count},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/debug/generate-toy")
def debug_generate_toy(x_kibana_url: str | None = Header(default=None)):
    """Proxy to log-generator toy scenario."""
    gen_url = _get_log_generator_url(x_kibana_url)
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{gen_url}/generate-toy")
        resp.raise_for_status()
        return resp.json()


@app.delete("/api/debug/logs")
def debug_delete_logs(x_kibana_url: str | None = Header(default=None)):
    """Proxy to log-generator DELETE /logs to avoid CORS."""
    gen_url = _get_log_generator_url(x_kibana_url)
    with httpx.Client(timeout=30) as client:
        resp = client.delete(f"{gen_url}/logs")
        resp.raise_for_status()
        return resp.json()


@app.post("/api/es/search")
def es_search_proxy(request_body: dict, x_kibana_url: str | None = Header(default=None)):
    """Thin ES search proxy for the debug UI."""
    index = request_body.get("index", "app-logs")
    body = request_body.get("body", {"query": {"match_all": {}}})
    es_client = _get_es_client(x_kibana_url)
    result = es_client.search(index=index, body=body)
    return dict(result)


@app.get("/api/transforms/{transform_id}")
def get_transform(transform_id: str, x_kibana_url: str | None = Header(default=None)):
    """Proxy to ES _transform API."""
    es_client = _get_es_client(x_kibana_url)
    result = es_client.transform.get_transform(transform_id=transform_id)
    return dict(result)


@app.post("/api/transforms/{transform_id}/schedule-now")
def schedule_transform_now(transform_id: str, x_kibana_url: str | None = Header(default=None)):
    """Trigger an immediate checkpoint for a continuous transform.

    Bypasses the frequency wait — the transform will check for new data right away.
    """
    es_client = _get_es_client(x_kibana_url)
    result = es_client.transform.schedule_now_transform(transform_id=transform_id)
    return dict(result)


@app.get("/debug", response_class=HTMLResponse)
def debug_ui():
    """Serve the debug walkthrough UI."""
    html_path = Path(__file__).parent / "debug_ui.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

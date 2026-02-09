"""Logs2Metrics API — LogMetricRule CRUD service."""

import logging
from datetime import datetime
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
from backend import BackendStatus
from connector_models import (
    DashboardDetail,
    DashboardSummary,
    FieldCardinality,
    IndexInfo,
    IndexMapping,
    IndexStats,
)
from config import ES_URL, KIBANA_URL
from database import create_db, get_session
from elastic_backend import backend as metrics_backend
from guardrails import EstimateResponse, GuardrailResult
from models import (
    LogMetricRule,
    RuleCreate,
    RuleResponse,
    RuleStatus,
    RuleUpdate,
)

log = logging.getLogger(__name__)

app = FastAPI(title="Logs2Metrics API", version="0.6.0")


@app.exception_handler(ESNotFoundError)
async def es_not_found_handler(request, exc):
    return JSONResponse(
        status_code=404,
        content={"detail": f"Elasticsearch resource not found: {exc}"},
    )


@app.exception_handler(HTTPStatusError)
async def kibana_error_handler(request, exc):
    if exc.response.status_code == 404:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Kibana resource not found"},
        )
    return JSONResponse(
        status_code=502,
        content={"detail": f"Kibana error: {exc.response.status_code}"},
    )


@app.on_event("startup")
def on_startup():
    create_db()


# ── Kibana connection dependency ──────────────────────────────────────


def get_kibana_conn(
    x_kibana_url: str | None = Header(default=None),
    x_kibana_user: str | None = Header(default=None),
    x_kibana_pass: str | None = Header(default=None),
) -> KibanaConnection | None:
    """Extract optional Kibana connection override from request headers."""
    if not x_kibana_url:
        return None
    return KibanaConnection(
        url=x_kibana_url.rstrip("/"),
        username=x_kibana_user,
        password=x_kibana_pass,
    )


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


@app.post("/api/debug/generate")
def debug_generate(request_body: dict):
    """Proxy to log-generator service to avoid CORS."""
    count = request_body.get("count", 10)
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "http://log-generator:8000/generate",
            json={"count": count},
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/api/debug/generate-toy")
def debug_generate_toy():
    """Proxy to log-generator toy scenario."""
    with httpx.Client(timeout=30) as client:
        resp = client.post("http://log-generator:8000/generate-toy")
        resp.raise_for_status()
        return resp.json()


@app.delete("/api/debug/logs")
def debug_delete_logs():
    """Proxy to log-generator DELETE /logs to avoid CORS."""
    with httpx.Client(timeout=30) as client:
        resp = client.delete("http://log-generator:8000/logs")
        resp.raise_for_status()
        return resp.json()


@app.post("/api/es/search")
def es_search_proxy(request_body: dict):
    """Thin ES search proxy for the debug UI."""
    index = request_body.get("index", "app-logs")
    body = request_body.get("body", {"query": {"match_all": {}}})
    result = _es.search(index=index, body=body)
    return dict(result)


@app.get("/api/transforms/{transform_id}")
def get_transform(transform_id: str):
    """Proxy to ES _transform API."""
    result = _es.transform.get_transform(transform_id=transform_id)
    return dict(result)


@app.get("/debug", response_class=HTMLResponse)
def debug_ui():
    """Serve the debug walkthrough UI."""
    html_path = Path(__file__).parent / "debug_ui.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

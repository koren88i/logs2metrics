"""Logs2Metrics API — LogMetricRule CRUD service."""

from datetime import datetime

from elasticsearch import NotFoundError as ESNotFoundError
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from httpx import HTTPStatusError
from sqlmodel import Session, select

import es_connector
import kibana_connector
from connector_models import (
    DashboardDetail,
    DashboardSummary,
    FieldCardinality,
    IndexInfo,
    IndexMapping,
    IndexStats,
)
from database import create_db, get_session
from models import (
    LogMetricRule,
    RuleCreate,
    RuleResponse,
    RuleUpdate,
)

app = FastAPI(title="Logs2Metrics API", version="0.2.0")


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


# ── CRUD endpoints ────────────────────────────────────────────────────


@app.post("/api/rules", response_model=RuleResponse, status_code=201)
def create_rule(body: RuleCreate, session: Session = Depends(get_session)):
    now = datetime.utcnow()
    rule = LogMetricRule(
        name=body.name,
        owner=body.owner,
        source=body.source.model_dump(),
        group_by=body.group_by.model_dump(),
        compute=body.compute.model_dump(),
        backend_config=body.backend_config.model_dump(),
        status=body.status.value,
        created_at=now,
        updated_at=now,
    )
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

    update_data = body.model_dump(exclude_unset=True)

    # Serialize nested Pydantic models to dicts for JSON columns
    for key in ("source", "group_by", "compute", "backend_config"):
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
    return RuleResponse.from_db(rule)


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, session: Session = Depends(get_session)):
    rule = session.get(LogMetricRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    session.delete(rule)
    session.commit()
    return None


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
def api_list_dashboards():
    return kibana_connector.list_dashboards()


@app.get("/api/kibana/dashboards/{dashboard_id}", response_model=DashboardDetail)
def api_get_dashboard(dashboard_id: str):
    return kibana_connector.get_dashboard_with_panels(dashboard_id)

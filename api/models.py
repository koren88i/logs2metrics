"""LogMetricRule domain models — Pydantic + SQLModel."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field
from sqlmodel import Column, Field as SQLField, SQLModel, JSON


# ── Enums ──────────────────────────────────────────────────────────────

class ComputeType(str, Enum):
    count = "count"
    sum = "sum"
    avg = "avg"
    distribution = "distribution"


class RuleStatus(str, Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    error = "error"


class BackendType(str, Enum):
    elastic = "elastic"


# ── Nested value objects (Pydantic, stored as JSON in SQLite) ─────────

class SourceConfig(BaseModel):
    index_pattern: str = Field(..., min_length=1, examples=["app-logs*"])
    time_field: str = Field(default="timestamp")
    filter_query: Optional[dict] = Field(
        default=None,
        description="Optional ES query DSL filter",
    )


class GroupByConfig(BaseModel):
    time_bucket: str = Field(default="1m", examples=["10s", "1m", "5m"])
    dimensions: list[str] = Field(
        default_factory=list,
        max_length=10,
        examples=[["service", "endpoint"]],
    )
    frequency: Optional[str] = Field(
        default=None,
        description="Check interval: how often the transform checks for new data (e.g. '1m', '5m'). "
                    "Defaults to max(time_bucket, 1m) if not set.",
        examples=["1m", "5m", "15m", "1h"],
    )
    sync_delay: str = Field(
        default="30s",
        description="Late data buffer: how long to wait for late-arriving events before sealing a time bucket. "
                    "Should exceed your worst-case log pipeline delay. Events arriving after this window "
                    "are silently dropped.",
        examples=["1s", "5s", "10s", "30s", "1m", "5m"],
    )


class ComputeConfig(BaseModel):
    type: ComputeType
    field: Optional[str] = Field(
        default=None,
        description="Required for sum/avg/distribution, ignored for count",
    )
    percentiles: Optional[list[float]] = Field(
        default=None,
        description="For distribution type, e.g. [50, 75, 90, 95, 99]",
    )


class BackendConfig(BaseModel):
    type: BackendType = BackendType.elastic
    retention_days: int = Field(default=450, ge=1, le=730)


class OriginConfig(BaseModel):
    dashboard_id: str = Field(..., description="Kibana dashboard ID")
    dashboard_title: str = Field(default="")
    panel_id: str = Field(..., description="Panel ID within the dashboard")
    panel_title: str = Field(default="")


# ── SQLModel table ────────────────────────────────────────────────────

class LogMetricRule(SQLModel, table=True):
    __tablename__ = "log_metric_rules"

    id: Optional[int] = SQLField(default=None, primary_key=True)
    name: str = SQLField(index=True)
    owner: str = SQLField(default="")

    # Nested configs stored as JSON columns
    source: dict = SQLField(default={}, sa_column=Column(JSON))
    group_by: dict = SQLField(default={}, sa_column=Column(JSON))
    compute: dict = SQLField(default={}, sa_column=Column(JSON))
    backend_config: dict = SQLField(default={}, sa_column=Column(JSON))
    origin: dict = SQLField(default={}, sa_column=Column(JSON))

    status: str = SQLField(default=RuleStatus.draft.value)

    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


# ── API schemas (request / response) ─────────────────────────────────

class RuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    owner: str = Field(default="")
    source: SourceConfig
    group_by: GroupByConfig = Field(default_factory=GroupByConfig)
    compute: ComputeConfig
    backend_config: BackendConfig = Field(default_factory=BackendConfig)
    origin: Optional[OriginConfig] = None
    status: RuleStatus = RuleStatus.draft


class RuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    owner: Optional[str] = None
    source: Optional[SourceConfig] = None
    group_by: Optional[GroupByConfig] = None
    compute: Optional[ComputeConfig] = None
    backend_config: Optional[BackendConfig] = None
    origin: Optional[OriginConfig] = None
    status: Optional[RuleStatus] = None


class RuleResponse(BaseModel):
    id: int
    name: str
    owner: str
    source: SourceConfig
    group_by: GroupByConfig
    compute: ComputeConfig
    backend_config: BackendConfig
    origin: Optional[OriginConfig] = None
    status: RuleStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, rule: LogMetricRule) -> "RuleResponse":
        return cls(
            id=rule.id,
            name=rule.name,
            owner=rule.owner,
            source=SourceConfig(**rule.source),
            group_by=GroupByConfig(**rule.group_by),
            compute=ComputeConfig(**rule.compute),
            backend_config=BackendConfig(**rule.backend_config),
            origin=OriginConfig(**rule.origin) if rule.origin else None,
            status=RuleStatus(rule.status),
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

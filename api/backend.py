"""Metrics backend interface and response models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field

from models import LogMetricRule

if TYPE_CHECKING:
    from elasticsearch import Elasticsearch


class TransformHealth(str, Enum):
    green = "green"
    yellow = "yellow"
    red = "red"
    stopped = "stopped"
    unknown = "unknown"


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class ProvisionResult(BaseModel):
    success: bool
    transform_id: str
    metrics_index: str
    ilm_policy: Optional[str] = None
    error: Optional[str] = None


class BackendStatus(BaseModel):
    rule_id: int
    transform_id: str
    health: TransformHealth
    docs_processed: int = 0
    docs_indexed: int = 0
    last_checkpoint: Optional[datetime] = None
    error: Optional[str] = None


class MetricsBackend(ABC):
    """Abstract interface for metrics backends."""

    @abstractmethod
    def validate(self, rule: LogMetricRule, es_client: Elasticsearch | None = None) -> ValidationResult: ...

    @abstractmethod
    def provision(self, rule: LogMetricRule, es_client: Elasticsearch | None = None) -> ProvisionResult: ...

    @abstractmethod
    def get_status(self, rule_id: int, es_client: Elasticsearch | None = None) -> BackendStatus: ...

    @abstractmethod
    def deprovision(self, rule_id: int, es_client: Elasticsearch | None = None) -> None: ...

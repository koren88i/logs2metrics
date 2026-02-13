"""Pydantic models for ES & Kibana connector responses."""

from pydantic import BaseModel, Field


# -- Elasticsearch connector models ------------------------------------


class IndexInfo(BaseModel):
    name: str
    doc_count: int
    store_size_bytes: int
    store_size_human: str


class FieldMapping(BaseModel):
    name: str
    type: str
    aggregatable: bool = True


class IndexMapping(BaseModel):
    index: str
    fields: list[FieldMapping]


class FieldCardinality(BaseModel):
    index: str
    field: str
    cardinality: int


class IndexStats(BaseModel):
    index: str
    doc_count: int
    store_size_bytes: int
    store_size_human: str
    query_total: int = 0
    query_time_ms: int = 0


# -- Kibana connector models -------------------------------------------


class DashboardSummary(BaseModel):
    id: str
    title: str
    description: str = ""


class MetricInfo(BaseModel):
    type: str
    field: str | None = None


class PanelAnalysis(BaseModel):
    panel_id: str
    title: str
    index_pattern: str | None = None
    time_field: str | None = None
    date_histogram_interval: str | None = None
    visualization_type: str
    agg_types: list[str] = Field(default_factory=list)
    metrics: list[MetricInfo] = Field(default_factory=list)
    group_by_fields: list[str] = Field(default_factory=list)
    has_raw_docs: bool = False
    filter_query: str | None = None


class DashboardDetail(BaseModel):
    id: str
    title: str
    description: str = ""
    panels: list[PanelAnalysis]

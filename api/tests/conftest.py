"""Shared fixtures for the logs2metrics test suite."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# ---- Environment setup (MUST happen before any api module import) ----
os.environ.setdefault("ES_URL", "http://localhost:9200")
os.environ.setdefault("KIBANA_URL", "http://kibana-test:5601")


# ── Pydantic model factories ─────────────────────────────────────────


@pytest.fixture
def make_rule_create():
    """Factory for RuleCreate instances with sensible defaults."""
    from models import RuleCreate, SourceConfig, GroupByConfig, ComputeConfig, ComputeType

    def _factory(**overrides):
        defaults = dict(
            name="test-rule",
            owner="test",
            source=SourceConfig(index_pattern="app-logs*"),
            group_by=GroupByConfig(time_bucket="1m", dimensions=["service"]),
            compute=ComputeConfig(type=ComputeType.count),
        )
        defaults.update(overrides)
        return RuleCreate(**defaults)

    return _factory


@pytest.fixture
def make_panel_analysis():
    """Factory for PanelAnalysis instances."""
    from connector_models import PanelAnalysis, MetricInfo

    def _factory(**overrides):
        defaults = dict(
            panel_id="panel-1",
            title="Test Panel",
            visualization_type="line",
            agg_types=["date_histogram", "count"],
            metrics=[MetricInfo(type="count", field=None)],
            group_by_fields=["service"],
            has_raw_docs=False,
        )
        defaults.update(overrides)
        return PanelAnalysis(**defaults)

    return _factory


@pytest.fixture
def make_field_mapping():
    """Factory for FieldMapping dicts keyed by name."""
    from connector_models import FieldMapping

    def _factory(fields=None):
        if fields is None:
            fields = {"service": "keyword", "endpoint": "keyword"}
        return {
            name: FieldMapping(name=name, type=ftype, aggregatable=(ftype != "text"))
            for name, ftype in fields.items()
        }

    return _factory


@pytest.fixture
def make_log_metric_rule():
    """Factory for LogMetricRule DB model instances (dict-based columns)."""
    from models import LogMetricRule, RuleStatus

    def _factory(**overrides):
        defaults = dict(
            id=1,
            name="test-rule",
            owner="test",
            source={"index_pattern": "app-logs*", "time_field": "timestamp", "filter_query": None},
            group_by={"time_bucket": "1m", "dimensions": ["service"], "frequency": None},
            compute={"type": "count", "field": None, "percentiles": None},
            backend_config={"type": "elastic", "retention_days": 450},
            origin={},
            status=RuleStatus.draft.value,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        defaults.update(overrides)
        return LogMetricRule(**defaults)

    return _factory


# ── Mock ES connector ─────────────────────────────────────────────────


@pytest.fixture
def mock_es_connector():
    """Patch es_connector module functions with sensible defaults."""
    from connector_models import IndexStats, FieldCardinality

    default_stats = IndexStats(
        index="app-logs",
        doc_count=100_000,
        store_size_bytes=500_000_000,
        store_size_human="476.8mb",
        query_total=0,
        query_time_ms=0,
    )
    default_cardinality = FieldCardinality(
        index="app-logs", field="service", cardinality=10,
    )

    with patch("es_connector.get_index_stats", return_value=default_stats) as m_stats, \
         patch("es_connector.get_field_cardinality", return_value=default_cardinality) as m_card:
        yield {
            "get_index_stats": m_stats,
            "get_field_cardinality": m_card,
            "default_stats": default_stats,
            "default_cardinality": default_cardinality,
        }


# ── Mock Elasticsearch client for elastic_backend ─────────────────────


@pytest.fixture
def mock_es_client():
    """Patch the module-level `es` Elasticsearch client in elastic_backend."""
    mock = MagicMock()
    with patch("elastic_backend.es", mock):
        yield mock


# ── FastAPI TestClient with in-memory DB ──────────────────────────────


@pytest.fixture
def test_client(mock_es_connector, mock_es_client):
    """FastAPI TestClient with in-memory SQLite and mocked backends."""
    from database import get_session
    from main import app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    mock_backend = MagicMock()
    mock_backend.provision.return_value = MagicMock(
        success=True, transform_id="l2m-rule-1",
        metrics_index="l2m-metrics-rule-1", ilm_policy="l2m-metrics-450d",
    )
    mock_backend.get_status.return_value = MagicMock(
        rule_id=1, transform_id="l2m-rule-1", health="green",
        docs_processed=100, docs_indexed=50, last_checkpoint=None, error=None,
    )
    mock_backend.deprovision.return_value = None

    with patch("main.metrics_backend", mock_backend):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        yield client, mock_backend

    app.dependency_overrides.clear()

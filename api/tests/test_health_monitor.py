"""Tests for the background health monitor (_check_all_active_rules)."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


# Environment setup
os.environ.setdefault("ES_URL", "http://localhost:9200")
os.environ.setdefault("KIBANA_URL", "http://kibana-test:5601")


def _make_engine_and_session():
    """Create an in-memory SQLite engine with the schema."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _insert_rule(session, *, rule_id=1, name="test-rule", status="active"):
    """Insert a rule directly into the DB."""
    from models import LogMetricRule
    rule = LogMetricRule(
        id=rule_id,
        name=name,
        owner="test",
        source={"index_pattern": "app-logs*", "time_field": "timestamp", "filter_query": None},
        group_by={"time_bucket": "1m", "dimensions": [], "frequency": None},
        compute={"type": "count", "field": None, "percentiles": None},
        backend_config={"type": "elastic", "retention_days": 450},
        origin={},
        status=status,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


class TestCheckAllActiveRules:
    def test_healthy_transform_keeps_rule_active(self):
        from backend import TransformHealth
        engine = _make_engine_and_session()

        mock_backend = MagicMock()
        mock_backend.get_status.return_value = MagicMock(
            health=TransformHealth.green, error=None,
        )

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, status="active")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules
            _check_all_active_rules()

        with Session(engine) as session:
            from models import LogMetricRule
            rule = session.get(LogMetricRule, 1)
            assert rule.status == "active"

    def test_red_transform_sets_rule_to_error(self):
        from backend import TransformHealth
        engine = _make_engine_and_session()

        mock_backend = MagicMock()
        mock_backend.get_status.return_value = MagicMock(
            health=TransformHealth.red, error="transform failed",
        )

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, status="active")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules
            _check_all_active_rules()

        with Session(engine) as session:
            from models import LogMetricRule
            rule = session.get(LogMetricRule, 1)
            assert rule.status == "error"

    def test_stopped_transform_sets_rule_to_error(self):
        from backend import TransformHealth
        engine = _make_engine_and_session()

        mock_backend = MagicMock()
        mock_backend.get_status.return_value = MagicMock(
            health=TransformHealth.stopped, error=None,
        )

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, status="active")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules
            _check_all_active_rules()

        with Session(engine) as session:
            from models import LogMetricRule
            rule = session.get(LogMetricRule, 1)
            assert rule.status == "error"

    def test_es_unreachable_does_not_crash(self):
        engine = _make_engine_and_session()

        mock_backend = MagicMock()
        mock_backend.get_status.side_effect = ConnectionError("ES unreachable")

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, status="active")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules
            # Should not raise
            _check_all_active_rules()

        # Rule should still be active (error was per-rule, not global)
        with Session(engine) as session:
            from models import LogMetricRule
            rule = session.get(LogMetricRule, 1)
            assert rule.status == "active"

    def test_draft_rules_not_checked(self):
        from backend import TransformHealth
        engine = _make_engine_and_session()

        mock_backend = MagicMock()

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, status="draft")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules
            _check_all_active_rules()

        mock_backend.get_status.assert_not_called()

    def test_multiple_rules_only_failing_updated(self):
        from backend import TransformHealth
        engine = _make_engine_and_session()

        def mock_get_status(rule_id):
            if rule_id == 3:
                return MagicMock(health=TransformHealth.red, error="failed")
            return MagicMock(health=TransformHealth.green, error=None)

        mock_backend = MagicMock()
        mock_backend.get_status.side_effect = mock_get_status

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, name="rule-1", status="active")
            _insert_rule(session, rule_id=2, name="rule-2", status="active")
            _insert_rule(session, rule_id=3, name="rule-3", status="active")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules
            _check_all_active_rules()

        with Session(engine) as session:
            from models import LogMetricRule
            assert session.get(LogMetricRule, 1).status == "active"
            assert session.get(LogMetricRule, 2).status == "active"
            assert session.get(LogMetricRule, 3).status == "error"

    def test_state_updated_after_check(self):
        from backend import TransformHealth
        engine = _make_engine_and_session()

        mock_backend = MagicMock()
        mock_backend.get_status.return_value = MagicMock(
            health=TransformHealth.green, error=None,
        )

        with Session(engine) as session:
            _insert_rule(session, rule_id=1, status="active")

        with patch("main.metrics_backend", mock_backend), \
             patch("database.engine", engine):
            from main import _check_all_active_rules, _health_monitor_state
            _check_all_active_rules()

        assert _health_monitor_state["last_check_time"] is not None
        assert _health_monitor_state["rules_in_error"] == []


class TestApiHealthEndpoint:
    def test_health_endpoint_returns_monitor_state(self, test_client):
        client, _ = test_client
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "monitor_running" in data
        assert "last_check_time" in data
        assert "rules_in_error" in data
        assert "check_interval_seconds" in data

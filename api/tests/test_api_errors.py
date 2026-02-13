"""Tests for error handling in the API layer."""

import pytest
from unittest.mock import MagicMock


class TestHealth:
    def test_health_endpoint(self, test_client):
        client, _ = test_client
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestProvisionFailure:
    def test_provision_failure_sets_error_status(self, test_client):
        client, mock_backend = test_client
        mock_backend.provision.return_value = MagicMock(
            success=False, transform_id="x", metrics_index="x", error="ES down",
        )
        resp = client.post("/api/rules", json={
            "name": "fail-provision",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "error"


class TestEstimateEndpoint:
    def test_estimate_returns_cost_and_guardrails(self, test_client):
        client, _ = test_client
        resp = client.post("/api/estimate", json={
            "name": "est-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "cost_estimate" in data
        assert "guardrails" in data
        assert "all_guardrails_passed" in data

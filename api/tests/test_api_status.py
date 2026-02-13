"""Tests for backend status endpoint and edge cases.

Bug 7 prevention: zero-match transforms must return valid status.
"""

import pytest
from unittest.mock import MagicMock


class TestGetRuleBackendStatus:
    def test_active_rule_returns_status(self, test_client):
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "status-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        rule_id = create_resp.json()["id"]
        resp = client.get(f"/api/rules/{rule_id}/status")
        assert resp.status_code == 200
        mock_backend.get_status.assert_called()

    def test_draft_rule_returns_400(self, test_client):
        client, _ = test_client
        create_resp = client.post("/api/rules", json={
            "name": "draft-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        rule_id = create_resp.json()["id"]
        resp = client.get(f"/api/rules/{rule_id}/status")
        assert resp.status_code == 400

    def test_nonexistent_rule_returns_404(self, test_client):
        client, _ = test_client
        resp = client.get("/api/rules/9999/status")
        assert resp.status_code == 404

    def test_zero_docs_processed_returns_valid_response(self, test_client):
        """Bug 7 prevention: transforms with 0 docs must return valid status."""
        client, mock_backend = test_client
        mock_backend.get_status.return_value = MagicMock(
            rule_id=1, transform_id="l2m-rule-1", health="green",
            docs_processed=0, docs_indexed=0, last_checkpoint=None, error=None,
        )
        create_resp = client.post("/api/rules", json={
            "name": "zero-match",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        rule_id = create_resp.json()["id"]
        resp = client.get(f"/api/rules/{rule_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["docs_processed"] == 0

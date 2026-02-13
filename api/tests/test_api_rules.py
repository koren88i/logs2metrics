"""Tests for rule CRUD endpoints via FastAPI TestClient."""

import pytest


class TestCreateRule:
    def test_create_draft_rule(self, test_client):
        client, mock_backend = test_client
        resp = client.post("/api/rules", json={
            "name": "test-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-rule"
        assert data["status"] == "draft"
        assert data["id"] is not None

    def test_create_active_rule_triggers_provision(self, test_client):
        client, mock_backend = test_client
        resp = client.post("/api/rules", json={
            "name": "active-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        assert resp.status_code == 201
        mock_backend.provision.assert_called_once()

    def test_create_rule_with_validation_error(self, test_client):
        client, _ = test_client
        resp = client.post("/api/rules", json={
            "name": "",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        assert resp.status_code == 422

    def test_create_rule_guardrail_failure_returns_422(self, test_client):
        client, _ = test_client
        resp = client.post("/api/rules", json={
            "name": "bad-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "group_by": {"dimensions": ["a", "b", "c", "d", "e", "f"]},
        })
        assert resp.status_code == 422

    def test_create_rule_skip_guardrails(self, test_client):
        client, _ = test_client
        resp = client.post("/api/rules?skip_guardrails=true", json={
            "name": "skip-guard-rule",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "group_by": {"dimensions": ["a", "b", "c", "d", "e", "f"]},
        })
        assert resp.status_code == 201


class TestListRules:
    def test_empty_list(self, test_client):
        client, _ = test_client
        resp = client.get("/api/rules")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_after_create(self, test_client):
        client, _ = test_client
        client.post("/api/rules", json={
            "name": "rule-1",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        resp = client.get("/api/rules")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestGetRule:
    def test_get_existing(self, test_client):
        client, _ = test_client
        create_resp = client.post("/api/rules", json={
            "name": "rule-get",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        rule_id = create_resp.json()["id"]
        resp = client.get(f"/api/rules/{rule_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "rule-get"

    def test_get_nonexistent_returns_404(self, test_client):
        client, _ = test_client
        resp = client.get("/api/rules/9999")
        assert resp.status_code == 404


class TestUpdateRule:
    def test_update_name(self, test_client):
        client, _ = test_client
        create_resp = client.post("/api/rules", json={
            "name": "original",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        rule_id = create_resp.json()["id"]
        resp = client.put(f"/api/rules/{rule_id}", json={"name": "updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated"

    def test_activate_triggers_provision(self, test_client):
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "rule-to-activate",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        rule_id = create_resp.json()["id"]
        mock_backend.provision.reset_mock()
        resp = client.put(f"/api/rules/{rule_id}", json={"status": "active"})
        assert resp.status_code == 200
        mock_backend.provision.assert_called_once()

    def test_deactivate_triggers_deprovision(self, test_client):
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "rule-to-deactivate",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        rule_id = create_resp.json()["id"]
        mock_backend.deprovision.reset_mock()
        resp = client.put(f"/api/rules/{rule_id}", json={"status": "paused"})
        assert resp.status_code == 200
        mock_backend.deprovision.assert_called_once()

    def test_config_change_on_active_rule_triggers_reprovision(self, test_client):
        """Changing group_by config on active rule must deprovision+reprovision."""
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "active-config-change",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        rule_id = create_resp.json()["id"]
        mock_backend.provision.reset_mock()
        mock_backend.deprovision.reset_mock()

        resp = client.put(f"/api/rules/{rule_id}", json={
            "group_by": {"time_bucket": "5m", "dimensions": [], "sync_delay": "1m"}
        })
        assert resp.status_code == 200
        mock_backend.deprovision.assert_called_once()
        mock_backend.provision.assert_called_once()

    def test_name_change_on_active_rule_does_not_reprovision(self, test_client):
        """Changing only name on active rule should NOT trigger reprovision."""
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "name-only-change",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        rule_id = create_resp.json()["id"]
        mock_backend.provision.reset_mock()
        mock_backend.deprovision.reset_mock()

        resp = client.put(f"/api/rules/{rule_id}", json={"name": "new-name"})
        assert resp.status_code == 200
        mock_backend.deprovision.assert_not_called()
        mock_backend.provision.assert_not_called()

    def test_update_nonexistent_returns_404(self, test_client):
        client, _ = test_client
        resp = client.put("/api/rules/9999", json={"name": "nope"})
        assert resp.status_code == 404


class TestDeleteRule:
    def test_delete_draft(self, test_client):
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "to-delete",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
        })
        rule_id = create_resp.json()["id"]
        mock_backend.deprovision.reset_mock()
        resp = client.delete(f"/api/rules/{rule_id}")
        assert resp.status_code == 204
        mock_backend.deprovision.assert_not_called()

    def test_delete_active_triggers_deprovision(self, test_client):
        client, mock_backend = test_client
        create_resp = client.post("/api/rules", json={
            "name": "active-delete",
            "source": {"index_pattern": "app-logs*"},
            "compute": {"type": "count"},
            "status": "active",
        })
        rule_id = create_resp.json()["id"]
        mock_backend.deprovision.reset_mock()
        resp = client.delete(f"/api/rules/{rule_id}")
        assert resp.status_code == 204
        mock_backend.deprovision.assert_called_once()

    def test_delete_nonexistent_returns_404(self, test_client):
        client, _ = test_client
        resp = client.delete("/api/rules/9999")
        assert resp.status_code == 404

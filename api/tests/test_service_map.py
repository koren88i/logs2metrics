"""Tests for _KIBANA_SERVICE_MAP auth parity and get_kibana_conn auto-fill.

Bug 3 prevention: every service map entry with es_auth must also have kibana_auth.
"""

import pytest


class TestServiceMapAuthParity:
    """Bug 3 prevention: verify auth keys are always paired."""

    def test_es_auth_implies_kibana_auth(self):
        from main import _KIBANA_SERVICE_MAP
        for url, svc in _KIBANA_SERVICE_MAP.items():
            if "es_auth" in svc:
                assert "kibana_auth" in svc, (
                    f"Service map entry '{url}' has es_auth but no kibana_auth. "
                    f"This will cause 401 errors on security-enabled Kibana. "
                    f"See Bug 3 in CLAUDE.md."
                )

    def test_kibana_auth_implies_es_auth(self):
        from main import _KIBANA_SERVICE_MAP
        for url, svc in _KIBANA_SERVICE_MAP.items():
            if "kibana_auth" in svc:
                assert "es_auth" in svc, (
                    f"Service map entry '{url}' has kibana_auth but no es_auth."
                )

    def test_all_entries_have_required_keys(self):
        from main import _KIBANA_SERVICE_MAP
        for url, svc in _KIBANA_SERVICE_MAP.items():
            assert "log_generator" in svc, f"Entry '{url}' missing log_generator"
            assert "es_url" in svc, f"Entry '{url}' missing es_url"


class TestGetKibanaConn:
    def test_known_url_auto_fills_auth(self):
        from main import get_kibana_conn
        conn = get_kibana_conn(
            x_kibana_url="http://kibana2:5601",
            x_kibana_user=None,
            x_kibana_pass=None,
        )
        assert conn is not None
        assert conn.url == "http://kibana2:5601"
        assert conn.username == "elastic"
        assert conn.password == "admin1"

    def test_unknown_url_no_auto_fill(self):
        from main import get_kibana_conn
        conn = get_kibana_conn(
            x_kibana_url="http://unknown:5601",
            x_kibana_user=None,
            x_kibana_pass=None,
        )
        assert conn is not None
        assert conn.username is None
        assert conn.password is None

    def test_explicit_auth_overrides_auto_fill(self):
        from main import get_kibana_conn
        conn = get_kibana_conn(
            x_kibana_url="http://kibana2:5601",
            x_kibana_user="custom_user",
            x_kibana_pass="custom_pass",
        )
        assert conn.username == "custom_user"
        assert conn.password == "custom_pass"

    def test_no_url_returns_none(self):
        from main import get_kibana_conn
        conn = get_kibana_conn(
            x_kibana_url=None,
            x_kibana_user=None,
            x_kibana_pass=None,
        )
        assert conn is None

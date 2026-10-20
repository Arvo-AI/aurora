"""A command the guardrail allows still dies when this caller cannot use the connector."""

import json

from chat.backend.agent.tools.cloud_exec_tool import connector_call_denied


def test_closed_connector_is_refused_before_login(monkeypatch):
    monkeypatch.setattr(
        "utils.auth.connector_access.may_use",
        lambda user_id, connector, scope_id="": False,
    )
    body = json.loads(connector_call_denied("uid-viewer", "azure", None, "resource list"))
    assert body["code"] == "CONNECTOR_DENIED"
    assert body["success"] is False
    assert "Access tab" in body["error"]


def test_azure_fanout_keeps_only_granted_subscriptions(monkeypatch):
    class _Access:
        def may_use(self, connector, scope_id=""):
            return scope_id == "sub-a"

    monkeypatch.setattr("utils.auth.connector_access.access_for", lambda user_id: _Access())
    from chat.backend.agent.tools.cloud_exec_tool import _granted_azure_connections

    kept = _granted_azure_connections("uid", [{"account_id": "sub-a"}, {"account_id": "sub-b"}])
    assert [row["account_id"] for row in kept] == ["sub-a"]


def test_open_connector_is_not_refused(monkeypatch):
    monkeypatch.setattr(
        "utils.auth.connector_access.may_use",
        lambda user_id, connector, scope_id="": True,
    )
    assert connector_call_denied("uid-viewer", "aws", None, "rds describe-db-instances") is None

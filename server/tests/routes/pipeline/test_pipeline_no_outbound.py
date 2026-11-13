"""Pipeline investigations must never post anywhere outside Aurora.

Covers every outbound path a background RCA has: start/complete/failed
notifications (email, Slack, Teams, Google Chat, PagerDuty, incident.io),
on_incident actions, Jira filing, third-party write tools, and correlation
onto another source's incident.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.backend.agent.access.outbound_policy import (
    OUTBOUND_WRITE_TOOLS,
    allows_outbound,
    drop_outbound_tools,
)

SERVER = pathlib.Path(__file__).resolve().parents[3]


def test_policy_blocks_pipeline_only():
    assert allows_outbound({"source": "pipeline"}) is False
    for other in ("grafana", "datadog", "jenkins", "slack", None):
        assert allows_outbound({"source": other}) is True


def test_third_party_write_tools_are_dropped_for_pipeline():
    names = ["post_slack_message", "post_teams_message", "jira_add_comment", "jira_create_issue",
             "servicenow_add_work_note", "github_commit", "sharepoint_create_page",
             "get_alert_field", "query_datadog", "elastic_search_logs", "web_search"]
    tools = [SimpleNamespace(name=n) for n in names]
    kept = {t.name for t in drop_outbound_tools(tools, "pipeline")}
    assert kept == {"get_alert_field", "query_datadog", "elastic_search_logs", "web_search"}
    assert not kept & OUTBOUND_WRITE_TOOLS
    # Other sources keep their tool list untouched.
    assert len(drop_outbound_tools(tools, "grafana")) == len(tools)


def test_cloud_tools_applies_the_filter():
    src = (SERVER / "chat" / "backend" / "agent" / "tools" / "cloud_tools.py").read_text(encoding="utf-8")
    assert "drop_outbound_tools(tools, _trigger_source)" in src
    assert "no_outbound={int(no_outbound)}" in src  # separate tool cache entry


def _parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]


def _guarded_by(call, name):
    node = call
    while getattr(node, "_parent", None) is not None:
        node = node._parent
        if isinstance(node, ast.If) and name in ast.unparse(node.test):
            return True
    return False


def test_every_on_incident_action_dispatch_is_guarded():
    tree = ast.parse((SERVER / "chat" / "background" / "task.py").read_text(encoding="utf-8"))
    _parents(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "dispatch_on_incident_actions"]
    assert len(calls) >= 3
    for call in calls:
        assert _guarded_by(call, "_allows_outbound"), f"unguarded dispatch at line {call.lineno}"


def test_completion_announcement_follows_send_notifications():
    tree = ast.parse((SERVER / "chat" / "background" / "task.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "delay"
             and getattr(n.func.value, "id", "") == "generate_incident_summary_from_chat"]
    assert calls
    for call in calls:
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert kw.get("announce_completion") == "send_notifications"


@pytest.fixture
def task(monkeypatch):
    import utils.cache.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)
    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    import chat.background.task as task_mod

    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    return task_mod


def test_jira_filing_is_skipped_for_pipeline_even_when_opted_in(task, monkeypatch):
    monkeypatch.setattr("routes.jira.jira_routes.jira_comment_back_enabled", lambda user_id: True)
    monkeypatch.setattr(task, "_session_has_successful_jira_action", lambda *a, **k: False)
    pipeline_ctx = {"integrations": {"jira": True}, "trigger_metadata": {"source": "pipeline"}}
    grafana_ctx = {"integrations": {"jira": True}, "trigger_metadata": {"source": "grafana"}}
    assert task._should_file_in_jira(pipeline_ctx, "s", "u") is False
    assert task._should_file_in_jira(grafana_ctx, "s", "u") is True


def test_pipeline_task_never_correlates_onto_other_incidents():
    src = (SERVER / "routes" / "pipeline" / "tasks.py").read_text(encoding="utf-8")
    assert "AlertCorrelator" not in src
    assert "apply_correlation_outcome" not in src
    assert "notify_" not in src.replace("SSE notify", "")

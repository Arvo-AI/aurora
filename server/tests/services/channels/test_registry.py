"""Provider-parameterised channel registry, exercised through the Slack wrappers
that call it: the SQL keeps the shape the Slack code relied on with ``provider``
bound instead of literal, and the outputs the agent sees are unchanged (golden)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from chat.backend.agent.tools import slack_tool
from routes.slack import slack_channels, slack_events_helpers
from services.channels import registry
from tests.services.channels import channels_golden as golden


def _db(fetchall_rows=None, fetchone_row=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = fetchall_rows if fetchall_rows is not None else []
    cur.fetchone.return_value = fetchone_row
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm, cur


def _patched(cm):
    return (
        patch.object(registry.db_pool, "get_admin_connection", return_value=cm),
        patch.object(registry, "set_rls_context", return_value="org-1"),
        patch.object(registry.org_scope, "resolve_org", return_value="org-1"),
        patch.object(registry.org_scope, "org_read_predicate", return_value=("org_id = %s", ("org-1",))),
    )


def test_slack_upsert_binds_provider_classifies_and_preserves_owner():
    cur = MagicMock()
    existing = {}
    ch = {"channel_id": "C1", "name": "pd-incident-42", "team_id": "T1", "is_member": True}
    assert slack_channels._upsert_channel(cur, "u1", "org", ch, existing) == ("C1", True)
    assert existing == {"C1": "u1"}
    sql, params = cur.execute.call_args.args
    flat = " ".join(sql.split())
    assert "'slack'" not in sql
    assert "ON CONFLICT (user_id, provider, channel_id) DO UPDATE SET" in flat
    assert "WHEN slack_channels.metadata_status IN ('pending', 'generating') THEN slack_channels.updated_at" in flat
    assert params[:5] == ("u1", "org", "slack", "T1", "C1")
    assert params[8:10] == ("incident", "pagerduty")  # classifier output still reaches the row
    assert json.loads(params[10]) == ch
    assert params[11] == "pending"

    # A channel another org member already owns keeps its owner and is not "new".
    cur = MagicMock()
    existing = {"C1": "owner"}
    assert slack_channels._upsert_channel(cur, "u2", "org", ch, existing, initial_status="skipped") == ("C1", False)
    params = cur.execute.call_args.args[1]
    assert params[0] == "owner"
    assert params[11] == "skipped"
    assert existing == {"C1": "owner"}


def test_get_connected_slack_channels_output_is_unchanged():
    for rows, expected in ((golden.CONNECTED_ROWS, golden.CONNECTED_TWO_ROWS_JSON),
                           ([], golden.CONNECTED_NO_ROWS_JSON)):
        cm, cur = _db(fetchall_rows=rows)
        p1, p2, p3, p4 = _patched(cm)
        with p1, p2, p3, p4:
            assert slack_tool.get_connected_slack_channels("u1") == expected
        sql, params = cur.execute.call_args.args
        assert " ".join(sql.split()) == golden.CONNECTED_SQL_FLAT.replace("provider = 'slack'", "provider = %s")
        assert params == ("slack", "org-1")
    assert slack_tool.get_connected_slack_channels(None) == golden.CONNECTED_NO_USER_JSON
    with patch.object(registry.org_scope, "resolve_org", side_effect=RuntimeError("db down")):
        assert json.loads(slack_tool.get_connected_slack_channels("u1"))["error"].startswith(
            "Failed to fetch connected Slack channels")


@pytest.mark.parametrize("row,expected", [((1,), True), (None, False)])
def test_is_member_channel_sql_and_tri_state(row, expected):
    cm, cur = _db(fetchone_row=row)
    p1, p2, p3, p4 = _patched(cm)
    with p1, p2, p3, p4:
        assert slack_tool._is_member_channel("u1", "C1") is expected
    sql, params = cur.execute.call_args.args
    assert "provider = %s" in sql
    assert "is_member" in sql
    assert "metadata_status" not in sql  # a description is not required to post
    assert params == ("slack", "C1", "org-1")
    with patch.object(registry.org_scope, "resolve_org", side_effect=RuntimeError("db down")):
        assert slack_tool._is_member_channel("u1", "C1") is None


def test_resolve_channel_label_format_and_fallback():
    cm, cur = _db(fetchone_row=("payments",))
    with patch.object(registry.db_pool, "get_admin_connection", return_value=cm), \
         patch.object(registry, "set_rls_context", return_value="org-1"):
        assert slack_events_helpers._resolve_channel_label("u1", "C1") == "#payments (C1)"
    sql, params = cur.execute.call_args.args
    assert "user_id = %s AND channel_id = %s AND provider = %s" in sql
    assert params == ("u1", "C1", "slack")
    cm, _ = _db(fetchone_row=None)
    with patch.object(registry.db_pool, "get_admin_connection", return_value=cm), \
         patch.object(registry, "set_rls_context", return_value="org-1"):
        assert slack_events_helpers._resolve_channel_label("u1", "C2") == "C2"
    assert slack_events_helpers._resolve_channel_label("u1", "") == "unknown"

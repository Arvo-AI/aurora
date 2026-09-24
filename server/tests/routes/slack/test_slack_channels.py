"""Unit tests for Slack channel classification and full-listing pagination."""

from unittest.mock import patch, MagicMock

from connectors.slack_connector.client import SlackClient
from routes.slack import slack_channels as mod
from routes.slack.slack_channels import _classify_channel, _rank_channels


def _ch(name="", topic="", purpose=""):
    return {
        "name": name,
        "topic": {"value": topic},
        "purpose": {"value": purpose},
    }


# --- _classify_channel heuristic -------------------------------------------

def test_incidentio_platform_detected_from_topic():
    ctype, platform = _classify_channel(_ch(name="inc-2024-payments", topic="Managed by incident.io"))
    assert ctype == "incident"
    assert platform == "incident.io"


def test_pagerduty_detected():
    ctype, platform = _classify_channel(_ch(name="pd-incident-42"))
    assert ctype == "incident"
    assert platform == "pagerduty"


def test_opsgenie_detected_from_purpose():
    _ctype, platform = _classify_channel(_ch(name="war-room", purpose="Opsgenie bridge"))
    assert platform == "opsgenie"


def test_incident_by_name_without_platform():
    ctype, platform = _classify_channel(_ch(name="incident-response"))
    assert ctype == "incident"
    assert platform is None


def test_alerting_channel_is_team():
    ctype, _platform = _classify_channel(_ch(name="payments-oncall"))
    assert ctype == "team"


def test_plain_channel_is_general():
    ctype, platform = _classify_channel(_ch(name="random"))
    assert ctype == "general"
    assert platform is None


def test_platform_name_embedded_in_unrelated_token_does_not_match():
    # Word-boundary matching: "opsgenie" as a substring of a larger token
    # (e.g. a URL host) must not be detected as the platform.
    _ctype, platform = _classify_channel(_ch(name="team", topic="see myopsgenies-notes"))
    assert platform is None


# --- _rank_channels ordering -----------------------------------------------

def test_rank_prioritizes_members_then_recency():
    channels = [
        {"id": "C_old_member", "is_member": True, "created": 100},
        {"id": "C_new_nonmember", "is_member": False, "created": 999},
        {"id": "C_new_member", "is_member": True, "created": 500},
        {"id": "C_old_nonmember", "is_member": False, "created": 50},
    ]
    ranked = [c["id"] for c in _rank_channels(channels)]
    # Members first (newest member before older member), then non-members by recency.
    assert ranked == ["C_new_member", "C_old_member", "C_new_nonmember", "C_old_nonmember"]


def test_rank_handles_missing_created_field():
    channels = [{"id": "C1", "is_member": True}, {"id": "C2", "is_member": False}]
    ranked = [c["id"] for c in _rank_channels(channels)]
    assert ranked == ["C1", "C2"]


# --- auto_register_channels: register all, describe only members ------------

def test_auto_register_describes_only_members():
    from unittest.mock import MagicMock

    # C1/C2 are members (describe), C3 is not (index-only, 'skipped') even though
    # it exists in the workspace list. describe_limit is a safety valve, not the
    # selector — membership is.
    members = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
    ]
    all_channels = [
        *members,
        {"id": "C3", "name": "three", "is_member": False, "created": 999},
    ]

    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members
    fake_client.list_all_channels.return_value = all_channels

    # Capture every _upsert_channel call's initial_status.
    upserts = []

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        upserts.append((ch["channel_id"], initial_status))
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], True  # all new

    enqueued = []

    # A context-manager-shaped DB connection stub.
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = []
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    # All 3 registered; only the 2 members described. The recent non-member C3 is
    # registered 'skipped' for awareness, NOT described.
    assert {u[0] for u in upserts} == {"C1", "C2", "C3"}
    assert dict(upserts)["C1"] == "pending"
    assert dict(upserts)["C2"] == "pending"
    assert dict(upserts)["C3"] == "skipped"
    assert set(enqueued) == {"C1", "C2"}
    assert described == 2


def test_auto_register_describe_limit_caps_members():
    """describe_limit still bounds an unusually large membership."""
    from unittest.mock import MagicMock

    members = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
        {"id": "C3", "name": "three", "is_member": True, "created": 100},
    ]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members
    fake_client.list_all_channels.return_value = members

    upserts = []

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        upserts.append((ch["channel_id"], initial_status))
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], True

    enqueued = []
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = []
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=2)

    # Only the 2 most-recent members described; the oldest member is 'skipped'.
    assert described == 2
    assert dict(upserts)["C3"] == "skipped"


def test_auto_register_skips_dismissed_channels():
    from unittest.mock import MagicMock

    channels = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
    ]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = channels
    fake_client.list_all_channels.return_value = channels

    upserts = []

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        upserts.append(ch["channel_id"])
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], False  # already-existing rows

    enqueued = []
    conn = MagicMock()
    cur = MagicMock()
    # C2 is already dismissed → (channel_id, user_id, is_dismissed).
    cur.fetchall.return_value = [("C1", "u", False), ("C2", "u", True)]
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    # Dismissed C2 is never upserted; only C1 is.
    assert upserts == ["C1"]
    assert enqueued == []  # C1 already existed, so nothing new to describe


def test_auto_register_prunes_channels_gone_from_slack():
    """A stored channel absent from a complete enumeration is deleted."""
    from unittest.mock import MagicMock

    # Slack now lists only C1; C_OLD was deleted/archived (or bot removed).
    channels = [{"id": "C1", "name": "one", "is_member": True, "created": 300}]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = channels
    fake_client.list_all_channels.return_value = channels

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], False

    conn = MagicMock()
    cur = MagicMock()
    # We have rows for C1 (still live) and C_OLD (gone).
    cur.fetchall.return_value = [("C1", "u", False), ("C_OLD", "u", False)]
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: None):
        mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    # A DELETE targeting exactly the stale id must have run.
    delete_calls = [c for c in cur.execute.call_args_list
                    if "DELETE FROM slack_channels" in c.args[0]]
    assert len(delete_calls) == 1
    assert delete_calls[0].args[1] == (["C_OLD"],)


def test_auto_register_does_not_prune_when_enumeration_truncated():
    """Hitting the cap means a partial list — never prune, or we'd delete real ones."""
    from unittest.mock import MagicMock

    # Exactly the cap → list is (assumed) truncated, so C_OLD must survive.
    channels = [{"id": f"C{i}", "name": str(i), "is_member": True, "created": i}
                for i in range(mod.LIST_CHANNELS_CAP)]
    fake_client = MagicMock()
    # Members fetched separately; empty here so the test focuses on prune-skip.
    fake_client.list_bot_channels.return_value = []
    fake_client.list_all_channels.return_value = channels

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], False

    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [("C_OLD", "u", False)]
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: None):
        mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    assert not any("DELETE FROM slack_channels" in c.args[0] for c in cur.execute.call_args_list)


# --- register_single_channel (member_joined_channel path) -------------------

def _single_reg_db(fetchone_row):
    """Build a (dbcm, cur) pair whose SELECT returns fetchone_row."""
    from unittest.mock import MagicMock
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_row
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    return dbcm, cur


def test_register_single_channel_registers_and_describes_new():
    from unittest.mock import MagicMock
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "inc-payments", "is_member": True}
    dbcm, _cur = _single_reg_db(None)  # no existing row
    enqueued = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", return_value=("C1", True)), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda u, c: enqueued.append(c)):
        assert mod.register_single_channel("u1", "C1", team_id="T1") is True
    assert enqueued == ["C1"]


def test_register_single_channel_restores_dismissed_on_reinvite():
    """A re-invite is the latest signal: restore (un-dismiss) and re-describe,
    even though no new row is created (returns False)."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "inc-payments", "is_member": True}
    dbcm, cur = _single_reg_db(("owner", True))  # existing + dismissed
    enqueued = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", return_value=("C1", False)) as up, \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda u, c: enqueued.append(c)):
        # No new row created → return False, but it's restored + re-described.
        assert mod.register_single_channel("u1", "C1", team_id="T1") is False
    up.assert_called_once()
    # An UPDATE that clears is_dismissed must have run.
    assert any("is_dismissed = FALSE" in call.args[0] for call in cur.execute.call_args_list)
    assert enqueued == ["C1"]


# --- _activate_channels (bulk activate) -------------------------------------

def _activate_db(returning_rows):
    """Build (dbcm, cur) where the UPDATE ... RETURNING yields returning_rows."""
    from unittest.mock import MagicMock
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = returning_rows
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    return dbcm, cur


def test_activate_channels_enqueues_only_returned_ids():
    # DB RETURNING drives what's enqueued: only rows the UPDATE actually touched
    # (existing ids) come back, so only those are described.
    dbcm, cur = _activate_db([("C1",), ("C2",)])
    enqueued = []
    # No Slack client → the best-effort join step is a no-op, keeping this test
    # focused on the enqueue/RETURNING contract.
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod, "get_slack_client_for_user", return_value=None), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda u, c: enqueued.append(c)):
        activated = mod._activate_channels("u1", ["C1", "C2", "C3"])

    assert activated == 2
    assert enqueued == ["C1", "C2"]
    # Activation supersedes dismissal: the UPDATE un-dismisses the rows it flips
    # (rather than excluding dismissed ones). Assert against the full call list
    # since the join step may add later, unrelated execute() calls.
    activate_calls = [
        c for c in cur.execute.call_args_list if "is_dismissed = FALSE" in c.args[0]
    ]
    assert activate_calls, "expected an UPDATE that clears is_dismissed"
    assert activate_calls[0].args[1] == (["C1", "C2", "C3"],)


def test_activate_channels_none_matched_enqueues_nothing():
    dbcm, _cur = _activate_db([])  # no rows matched (all unknown/dismissed)
    enqueued = []
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod, "get_slack_client_for_user", return_value=None), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda u, c: enqueued.append(c)):
        activated = mod._activate_channels("u1", ["C_gone"])

    assert activated == 0
    assert enqueued == []


def test_activate_channels_joins_and_marks_member():
    # Activating a public channel should conversations.join it and flip is_member
    # so the UI reflects real Slack membership, not just the Active flag.
    dbcm, cur = _activate_db([("C1",), ("C2",)])
    client = MagicMock()
    client.join_channel.return_value = {"id": "ok"}  # join succeeds for both
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_enqueue_metadata"):
        mod._activate_channels("u1", ["C1", "C2"])

    # Both activated channels were joined.
    assert sorted(c.args[0] for c in client.join_channel.call_args_list) == ["C1", "C2"]
    # An UPDATE marking is_member = TRUE ran for the joined ids.
    member_calls = [
        c for c in cur.execute.call_args_list if "is_member = TRUE" in c.args[0]
    ]
    assert member_calls, "expected an UPDATE setting is_member = TRUE"
    assert member_calls[0].args[1] == (["C1", "C2"],)


def test_activate_channels_join_failure_does_not_mark_member():
    # A private channel can't be self-joined (join_channel returns None); activation
    # still proceeds but no is_member UPDATE should run.
    dbcm, cur = _activate_db([("C1",)])
    client = MagicMock()
    client.join_channel.return_value = None  # join fails (e.g. private channel)
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_enqueue_metadata"):
        activated = mod._activate_channels("u1", ["C1"])

    assert activated == 1  # activation not blocked by join failure
    assert not any("is_member = TRUE" in c.args[0] for c in cur.execute.call_args_list)


# --- list_all_channels pagination ------------------------------------------

def test_list_all_channels_paginates_until_cursor_empty():
    client = SlackClient("xoxb-test")
    pages = [
        {"ok": True, "channels": [{"id": "C1"}], "response_metadata": {"next_cursor": "abc"}},
        {"ok": True, "channels": [{"id": "C2"}], "response_metadata": {"next_cursor": ""}},
    ]
    calls = []

    def fake_request(method, endpoint, data=None, **kwargs):
        assert (method, endpoint) == ("GET", "conversations.list")
        calls.append(data.get("cursor"))
        return pages.pop(0)

    with patch.object(client, "_make_request", side_effect=fake_request):
        result = client.list_all_channels()

    assert [c["id"] for c in result] == ["C1", "C2"]
    # First call has no cursor, second passes the cursor from page 1.
    assert calls == [None, "abc"]


def test_list_all_channels_respects_safety_cap():
    client = SlackClient("xoxb-test")

    def fake_request(method, endpoint, data=None, **kwargs):
        # Always returns a full page with a cursor — would loop forever without the cap.
        return {
            "ok": True,
            "channels": [{"id": f"C{i}"} for i in range(200)],
            "response_metadata": {"next_cursor": "more"},
        }

    with patch.object(client, "_make_request", side_effect=fake_request):
        result = client.list_all_channels(max_channels=300)

    # Stops once the cap is reached and returns exactly the cap (not a full page over it).
    assert len(result) == 300


# --- card channel: helpers + dismiss guard ----------------------------------

def test_is_active_channel_true_only_for_ready_undismissed():
    from unittest.mock import MagicMock
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (1,)  # a matching ready/undismissed row
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        assert mod._is_active_channel("u1", "C1") is True
    # The guard predicate must require ready + not dismissed.
    sql = cur.execute.call_args.args[0]
    assert "metadata_status = 'ready'" in sql
    assert "NOT is_dismissed" in sql


def test_is_active_channel_false_when_no_row():
    from unittest.mock import MagicMock
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        assert mod._is_active_channel("u1", "C1") is False


def test_dismiss_card_channel_clears_designation_and_proceeds():
    """Deactivating the card channel is allowed: it clears the card designation
    (so the card has no stale destination) and still dismisses the row."""
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    with patch.object(mod, "_get_card_channel_id", return_value="C_CARD"), \
         patch.object(mod, "_clear_card_channel") as clear, \
         patch.object(mod, "_update_one_channel", return_value=None) as upd:
        with app.test_request_context("/slack/channels/C_CARD/dismiss", method="POST"):
            resp = mod.dismiss_slack_channel.__wrapped__("u1", "C_CARD")
    assert resp.get_json()["is_dismissed"] is True
    clear.assert_called_once_with("u1")  # card designation cleared
    upd.assert_called_once()              # and the row is dismissed


def test_dismiss_non_card_channel_does_not_touch_card():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    with patch.object(mod, "_get_card_channel_id", return_value="C_CARD"), \
         patch.object(mod, "_clear_card_channel") as clear, \
         patch.object(mod, "_update_one_channel", return_value=None) as upd:
        with app.test_request_context("/slack/channels/C_OTHER/dismiss", method="POST"):
            resp = mod.dismiss_slack_channel.__wrapped__("u1", "C_OTHER")
    assert resp.get_json()["is_dismissed"] is True
    clear.assert_not_called()  # a non-card channel never clears the card
    upd.assert_called_once()

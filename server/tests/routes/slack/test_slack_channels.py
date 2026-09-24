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


def _db(fetchall_rows=None, fetchone_row=None):
    """Build a (dbcm, cur) context-manager-shaped DB stub."""
    conn = MagicMock()
    cur = MagicMock()
    if fetchall_rows is not None:
        cur.fetchall.return_value = fetchall_rows
    if fetchone_row is not None or fetchall_rows is None:
        cur.fetchone.return_value = fetchone_row
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    return dbcm, cur


# --- auto_register_channels: persist only members, prune non-members --------

def test_auto_register_persists_only_members():
    # Only member channels are stored/described; the workspace is NOT enumerated
    # here (that's done live on read), so list_all_channels must not be called.
    members = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
    ]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members

    upserts = []

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        upserts.append((ch["channel_id"], initial_status))
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], True

    enqueued = []
    dbcm, _cur = _db(fetchall_rows=[])  # no existing rows

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    assert {u[0] for u in upserts} == {"C1", "C2"}
    assert set(enqueued) == {"C1", "C2"}
    assert described == 2
    fake_client.list_all_channels.assert_not_called()


def test_auto_register_describe_limit_caps_members():
    """describe_limit bounds how many descriptions we ENQUEUE per pass, but under
    membership-as-truth every member channel stays 'pending' (never 'skipped') so
    a later pass finishes describing it and it becomes routable."""
    members = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
        {"id": "C3", "name": "three", "is_member": True, "created": 100},
    ]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members

    upserts = []
    enqueued = []

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        upserts.append((ch["channel_id"], initial_status))
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], True

    dbcm, _cur = _db(fetchall_rows=[])

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=2)

    # Only the 2 most-recent members are described THIS pass...
    assert described == 2
    assert set(enqueued) == {"C1", "C2"}
    # ...but the oldest member is still persisted 'pending' (not 'skipped'), so a
    # subsequent reconcile can describe it — it's never permanently invisible.
    assert dict(upserts)["C3"] == "pending"


def _run_reconcile_with_existing(rows):
    """Run auto_register for a single member C1 whose stored row is `rows[0]`,
    returning the list of channel_ids enqueued for description."""
    members = [{"id": "C1", "name": "one", "is_member": True, "created": 300}]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members
    enqueued = []

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        existing[ch["channel_id"]] = user_id
        # Existing row -> is_new False (the interesting re-enqueue path).
        return ch["channel_id"], False

    dbcm, _cur = _db(fetchall_rows=rows)
    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_get_card_channel_id", return_value=None), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)
    return enqueued


def test_reconcile_skips_fresh_pending_but_reenqueues_stale():
    """A freshly-queued 'pending' row (task in flight) is NOT re-enqueued; a
    stale one (worker died) IS — so we don't spam duplicate LLM calls."""
    # (channel_id, user_id, status, is_stale)
    assert _run_reconcile_with_existing([("C1", "u", "pending", False)]) == []
    assert _run_reconcile_with_existing([("C1", "u", "pending", True)]) == ["C1"]


def test_reconcile_does_not_reenqueue_error_or_ready_or_generating():
    """'error' is left for an explicit regenerate (the task's own retries bound
    transient failures); 'ready'/'generating' are already done/in progress."""
    for status in ("error", "ready", "generating"):
        assert _run_reconcile_with_existing([("C1", "u", status, True)]) == [], status


def test_reconcile_reenqueues_skipped():
    """A 'skipped' row (legacy over-cap) is always re-enqueued so it reaches ready."""
    assert _run_reconcile_with_existing([("C1", "u", "skipped", False)]) == ["C1"]


def test_auto_register_prunes_non_member_channels():
    """A stored row Aurora is no longer a member of is pruned (self-healing)."""
    members = [{"id": "C1", "name": "one", "is_member": True, "created": 300}]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], False

    # Stored rows: C1 (still a member) and C_OLD (no longer a member).
    # 4-tuple: (channel_id, user_id, metadata_status, is_stale).
    dbcm, cur = _db(fetchall_rows=[("C1", "u", "ready", False), ("C_OLD", "u", "ready", False)])

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_get_card_channel_id", return_value=None), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: None):
        mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    delete_calls = [c for c in cur.execute.call_args_list
                    if "DELETE FROM slack_channels" in c.args[0]]
    assert len(delete_calls) == 1
    assert delete_calls[0].args[1] == (["C_OLD"],)


# --- register_single_channel (member_joined_channel path) -------------------

def test_register_single_channel_registers_and_describes_new():
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "inc-payments", "is_member": True}
    dbcm, _cur = _db(fetchone_row=None)  # no existing row
    enqueued = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", return_value=("C1", True)), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda u, c: enqueued.append(c)):
        assert mod.register_single_channel("u1", "C1", team_id="T1") is True
    assert enqueued == ["C1"]


def test_register_single_channel_marks_member_true():
    """A joined channel is always registered as a member (is_member=True)."""
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "inc-payments"}
    dbcm, _cur = _db(fetchone_row=None)
    captured = {}

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        captured["is_member"] = ch.get("is_member")
        return "C1", True

    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata"):
        mod.register_single_channel("u1", "C1", team_id="T1")

    assert captured["is_member"] is True


# --- _activate_channels (join-based) ----------------------------------------

def test_activate_channels_joins_and_registers():
    # Activate = join each channel, then register it as a member channel.
    client = MagicMock()
    client.join_channel.return_value = {"id": "ok"}  # join succeeds
    registered = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "register_single_channel",
                      side_effect=lambda u, c: registered.append(c)):
        activated = mod._activate_channels("u1", ["C1", "C2"])

    assert activated == 2
    assert sorted(c.args[0] for c in client.join_channel.call_args_list) == ["C1", "C2"]
    assert registered == ["C1", "C2"]


def test_activate_channels_skips_unjoinable():
    # A channel that can't be joined (e.g. private) is skipped, not registered.
    client = MagicMock()
    client.join_channel.side_effect = lambda cid: {"id": cid} if cid == "C1" else None
    registered = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "register_single_channel",
                      side_effect=lambda u, c: registered.append(c)):
        activated = mod._activate_channels("u1", ["C1", "C2_private"])

    assert activated == 1
    assert registered == ["C1"]


def test_activate_channels_no_client_returns_zero():
    with patch.object(mod, "get_slack_client_for_user", return_value=None):
        assert mod._activate_channels("u1", ["C1"]) == 0


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
    assert calls == [None, "abc"]


def test_list_all_channels_respects_safety_cap():
    client = SlackClient("xoxb-test")

    def fake_request(method, endpoint, data=None, **kwargs):
        return {
            "ok": True,
            "channels": [{"id": f"C{i}"} for i in range(200)],
            "response_metadata": {"next_cursor": "more"},
        }

    with patch.object(client, "_make_request", side_effect=fake_request):
        result = client.list_all_channels(max_channels=300)

    assert len(result) == 300


# --- get_slack_channels: connected=members, dismissed=available -------------

def test_list_available_channels_excludes_members():
    client = MagicMock()
    client.list_all_channels.return_value = [
        {"id": "C1", "name": "one"},          # already a member -> excluded
        {"id": "C2", "name": "two"},          # not a member -> included
        {"id": "C3", "name": "three"},        # not a member -> included
    ]
    with patch.object(mod, "get_slack_client_for_user", return_value=client):
        available = mod._list_available_channels("u1", {"C1"})

    ids = {c["channel_id"] for c in available}
    assert ids == {"C2", "C3"}
    # Available entries carry is_dismissed=True so the frontend shows them Inactive.
    assert all(c["is_dismissed"] is True for c in available)
    assert all(c["is_member"] is False for c in available)


def test_list_available_channels_no_client_returns_empty():
    with patch.object(mod, "get_slack_client_for_user", return_value=None):
        assert mod._list_available_channels("u1", set()) == []


# --- _is_active_channel: membership-based -----------------------------------

def test_is_active_channel_true_for_member_row():
    dbcm, cur = _db(fetchone_row=(1,))
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        assert mod._is_active_channel("u1", "C1") is True
    sql = cur.execute.call_args.args[0]
    assert "is_member" in sql


def test_is_active_channel_false_when_no_row():
    dbcm, _cur = _db(fetchone_row=None)
    with patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        assert mod._is_active_channel("u1", "C1") is False


# --- deactivate = leave, restore = join -------------------------------------

def test_deactivate_leaves_channel_and_prunes_row():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    client = MagicMock()
    dbcm, cur = _db(fetchall_rows=[])
    with patch.object(mod, "_get_card_channel_id", return_value=None), \
         patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        with app.test_request_context("/slack/channels/C1/dismiss", method="POST"):
            resp = mod.dismiss_slack_channel.__wrapped__("u1", "C1")
    assert resp.get_json()["is_dismissed"] is True
    client.leave_channel.assert_called_once_with("C1")  # actually leaves Slack
    # and prunes the local row
    assert any("DELETE FROM slack_channels" in c.args[0] for c in cur.execute.call_args_list)


def test_deactivate_card_channel_clears_designation():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    client = MagicMock()
    dbcm, _cur = _db(fetchall_rows=[])
    with patch.object(mod, "_get_card_channel_id", return_value="C_CARD"), \
         patch.object(mod, "_clear_card_channel") as clear, \
         patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        with app.test_request_context("/slack/channels/C_CARD/dismiss", method="POST"):
            mod.dismiss_slack_channel.__wrapped__("u1", "C_CARD")
    clear.assert_called_once_with("u1")


def test_deactivate_keeps_row_and_409s_when_leave_fails():
    """If Aurora can't leave (e.g. #general), don't prune the row — otherwise the
    next reconcile re-adds it and the channel flaps. Report a 409 instead."""
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    client = MagicMock()
    client.leave_channel.return_value = False  # leave genuinely failed
    dbcm, cur = _db(fetchall_rows=[])
    with patch.object(mod, "_get_card_channel_id", return_value=None), \
         patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        with app.test_request_context("/slack/channels/C1/dismiss", method="POST"):
            resp = mod.dismiss_slack_channel.__wrapped__("u1", "C1")
    body, status = resp
    assert status == 409
    assert body.get_json()["code"] == "leave_failed"
    # Row must NOT be pruned when the leave failed.
    assert not any("DELETE FROM slack_channels" in c.args[0] for c in cur.execute.call_args_list)


def test_deactivate_does_not_clear_card_when_leave_fails():
    """If the leave fails (409), the channel is still Active — the card
    designation must NOT be cleared, or the card silently stops posting."""
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    client = MagicMock()
    client.leave_channel.return_value = False  # e.g. cant_leave_general
    dbcm, _cur = _db(fetchall_rows=[])
    with patch.object(mod, "_get_card_channel_id", return_value="C1"), \
         patch.object(mod, "_clear_card_channel") as clear, \
         patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        with app.test_request_context("/slack/channels/C1/dismiss", method="POST"):
            resp = mod.dismiss_slack_channel.__wrapped__("u1", "C1")
    assert resp[1] == 409
    clear.assert_not_called()


def test_auto_register_clears_card_channel_when_pruned():
    """A pruned channel that was the card channel gets its designation cleared,
    so the resolver doesn't keep serving a dead destination."""
    members = [{"id": "C1", "name": "one", "is_member": True, "created": 300}]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], False

    # C_CARD is stored, is the card channel, and is no longer a member -> pruned.
    # 4-tuple: (channel_id, user_id, metadata_status, is_stale).
    dbcm, _cur = _db(fetchall_rows=[("C1", "u", "ready", False), ("C_CARD", "u", "ready", False)])

    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_get_card_channel_id", return_value="C_CARD"), \
         patch.object(mod, "_clear_card_channel") as clear, \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: None):
        mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=50)

    clear.assert_called_once_with("11111111-1111-1111-1111-111111111111")


def test_get_channels_poll_mode_skips_slack():
    """?live=0 (status poll) reads stored rows only — no membership reconcile and
    no live available-channel listing (the rate-limited Slack calls)."""
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    dbcm, _cur = _db(fetchall_rows=[])
    with patch.object(mod, "auto_register_channels") as reconcile, \
         patch.object(mod, "_list_available_channels") as avail, \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "org_read_predicate", return_value=("user_id = %s", ["u1"])), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod, "_get_card_channel_id", return_value=None), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        with app.test_request_context("/slack/channels?live=0", method="GET"):
            resp = mod.get_slack_channels.__wrapped__("u1")
    # Neither Slack path runs in poll mode.
    reconcile.assert_not_called()
    avail.assert_not_called()
    assert resp.get_json()["dismissed"] == []


def test_get_channels_full_load_reconciles_and_lists():
    """A normal load (no ?live=0) reconciles membership and live-lists available."""
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    dbcm, _cur = _db(fetchall_rows=[])
    with patch.object(mod, "auto_register_channels") as reconcile, \
         patch.object(mod, "_list_available_channels", return_value=[]) as avail, \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "org_read_predicate", return_value=("user_id = %s", ["u1"])), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod, "_get_card_channel_id", return_value=None), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm):
        with app.test_request_context("/slack/channels", method="GET"):
            mod.get_slack_channels.__wrapped__("u1")
    reconcile.assert_called_once_with("u1")
    avail.assert_called_once()


def test_restore_joins_channel_and_registers():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    client = MagicMock()
    client.join_channel.return_value = {"id": "C1"}
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "register_single_channel") as reg:
        with app.test_request_context("/slack/channels/C1/restore", method="POST"):
            resp = mod.restore_slack_channel.__wrapped__("u1", "C1")
    assert resp.get_json()["is_dismissed"] is False
    client.join_channel.assert_called_once_with("C1")
    reg.assert_called_once_with("u1", "C1")


def test_restore_join_failure_returns_409():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mod.slack_channels_bp, url_prefix="/slack")
    client = MagicMock()
    client.join_channel.return_value = None  # private channel can't self-join
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "register_single_channel") as reg:
        with app.test_request_context("/slack/channels/C1/restore", method="POST"):
            resp = mod.restore_slack_channel.__wrapped__("u1", "C1")
    body, status = resp
    assert status == 409
    assert body.get_json()["code"] == "join_failed"
    reg.assert_not_called()

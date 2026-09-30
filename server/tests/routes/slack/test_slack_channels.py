"""Unit tests for Slack channel classification and full-listing pagination."""

import json
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
         patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
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
         patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=2)

    # Only the 2 most-recent members are described THIS pass...
    assert described == 2
    assert set(enqueued) == {"C1", "C2"}
    # ...but the oldest member is still persisted 'pending' (not 'skipped'), so a
    # subsequent reconcile can describe it — it's never permanently invisible.
    assert dict(upserts)["C3"] == "pending"


# --- _mark_pending (claiming rows for description) --------------------------

def test_mark_pending_returns_only_claimed_ids():
    """Callers enqueue what the UPDATE actually claimed, so a row someone else
    already took doesn't get a duplicate (wasted) LLM call."""
    cur = MagicMock()
    cur.fetchall.return_value = [("C1",)]
    assert mod._mark_pending(cur, ["C1", "C2"]) == ["C1"]


def test_mark_pending_noops_on_empty_list():
    cur = MagicMock()
    assert mod._mark_pending(cur, []) == []
    cur.execute.assert_not_called()


def test_mark_pending_without_staleness_claims_fresh_rows():
    """The reconcile pass also enqueues rows it just inserted, so it must not be
    gated on a staleness window."""
    cur = MagicMock()
    cur.fetchall.return_value = [("C1",)]
    mod._mark_pending(cur, ["C1"])
    sql = cur.execute.call_args.args[0]
    assert "make_interval" not in sql
    assert "metadata_status IN ('pending', 'skipped')" in sql


def test_mark_pending_with_staleness_reasserts_the_window():
    """The backfill re-checks the predicate its SELECT observed, making
    select-then-claim atomic against a racing sweep."""
    cur = MagicMock()
    cur.fetchall.return_value = [("C1",)]
    mod._mark_pending(cur, ["C1"], stale_minutes=15)
    sql, params = cur.execute.call_args.args
    assert "make_interval" in sql
    assert params == (["C1"], 15)


def test_mark_pending_always_restamps_updated_at():
    """updated_at is the 'time since queued' signal; without restamping, a
    just-queued row looks stale and gets enqueued again."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    mod._mark_pending(cur, ["C1"])
    assert "updated_at = NOW()" in cur.execute.call_args.args[0]


def test_reconcile_enqueues_only_claimed_rows():
    """auto_register_channels must honor the claim result too."""
    members = [{"id": "C1", "name": "one", "is_member": True, "created": 300}]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members
    enqueued = []

    dbcm, _cur = _db(fetchall_rows=[])
    with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
         patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", return_value=("C1", True)), \
         patch.object(mod, "_mark_pending", return_value=[]), \
         patch.object(mod, "_enqueue_metadata", side_effect=lambda u, c: enqueued.append(c)):
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111")

    # The claim came back empty (another pass won it), so nothing is enqueued.
    assert enqueued == []
    assert described == 0


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
         patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
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


def test_over_cap_member_is_picked_up_on_a_later_pass():
    """A member ranked past describe_limit isn't described on the first pass, but
    once its 'pending' row goes stale a later reconcile enqueues it (so it's never
    permanently invisible to the agent)."""
    members = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
        {"id": "C3", "name": "three", "is_member": True, "created": 100},
    ]
    fake_client = MagicMock()
    fake_client.list_bot_channels.return_value = members

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        # Simulate all rows already existing (so describe decisions ride on status).
        existing[ch["channel_id"]] = user_id
        return ch["channel_id"], False

    def run(rows):
        enqueued = []
        dbcm, _cur = _db(fetchall_rows=rows)
        with patch.object(mod, "get_slack_client_for_user", return_value=fake_client), \
             patch.object(mod, "resolve_org", return_value="00000000-0000-0000-0000-000000000000"), \
             patch.object(mod, "set_rls_context", return_value="org"), \
             patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
             patch.object(mod, "_get_card_channel_id", return_value=None), \
             patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
             patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
             patch.object(mod, "_enqueue_metadata", side_effect=lambda uid, cid: enqueued.append(cid)):
            mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=2)
        return enqueued

    # First pass: C1/C2 fresh pending (in flight, skipped), C3 also pending but
    # NOT stale yet -> nothing enqueued (cap doesn't matter, none are eligible).
    first = run([("C1", "u", "pending", False), ("C2", "u", "pending", False),
                 ("C3", "u", "pending", False)])
    assert first == []

    # Later pass: C1/C2 finished (ready), C3's pending row has gone stale ->
    # the over-cap channel is finally enqueued.
    later = run([("C1", "u", "ready", False), ("C2", "u", "ready", False),
                 ("C3", "u", "pending", True)])
    assert later == ["C3"]


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
         patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
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
         patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
         patch.object(mod, "_enqueue_metadata"):
        mod.register_single_channel("u1", "C1", team_id="T1")

    assert captured["is_member"] is True


def test_register_single_channel_resolves_the_workspace_when_not_given():
    """The activate/restore routes don't pass a team_id, but the row still needs
    one: the description backfill uses it to reject a stand-in actor whose token
    belongs to a different Slack workspace."""
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "inc-payments"}
    dbcm, _cur = _db(fetchone_row=None)
    captured = {}

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        captured["team_id"] = ch.get("team_id")
        return "C1", True

    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user", return_value="T_RESOLVED"), \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata"):
        mod.register_single_channel("u1", "C1")  # no team_id from the caller

    assert captured["team_id"] == "T_RESOLVED"


def test_register_single_channel_prefers_the_callers_workspace():
    """The member_joined_channel event carries the authoritative team_id — the row
    must be tagged with that, not with a re-derived value."""
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "inc-payments"}
    dbcm, _cur = _db(fetchone_row=None)
    captured = {}

    def fake_upsert(cur, user_id, org_id, ch, existing, initial_status="pending"):
        captured["team_id"] = ch.get("team_id")
        return "C1", True

    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user", return_value="T_DERIVED"), \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", side_effect=fake_upsert), \
         patch.object(mod, "_enqueue_metadata"):
        mod.register_single_channel("u1", "C1", team_id="T_FROM_EVENT")

    assert captured["team_id"] == "T_FROM_EVENT"


# --- _activate_channels (join-based) ----------------------------------------

def test_activate_channels_joins_and_registers():
    # Activate = join each channel, then register it as a member channel.
    client = MagicMock()
    client.join_channel.return_value = {"id": "ok"}  # join succeeds
    registered = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "register_single_channel",
                      side_effect=lambda u, c, team_id=None: registered.append(c)):
        activated = mod._activate_channels("u1", ["C1", "C2"])

    assert activated == 2
    assert sorted(c.args[0] for c in client.join_channel.call_args_list) == ["C1", "C2"]
    assert registered == ["C1", "C2"]


def test_activate_channels_tags_rows_with_the_workspace():
    # Rows must carry team_id so the description backfill can tell which org
    # member's token is able to read the channel — an org can connect several
    # workspaces, and a token from the wrong one reads nothing. Resolved once for
    # the batch, not once per channel (it's a DB + Vault round trip).
    client = MagicMock()
    client.join_channel.return_value = {"id": "ok"}
    teams = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user", return_value="T1") as probe, \
         patch.object(mod, "register_single_channel",
                      side_effect=lambda u, c, team_id=None: teams.append(team_id)):
        mod._activate_channels("u1", ["C1", "C2", "C3"])

    assert teams == ["T1", "T1", "T1"]
    assert probe.call_count == 1


def test_register_single_channel_reuses_the_workspace_for_invalidation():
    """Cache invalidation must not re-resolve the workspace: a bulk activate calls
    register_single_channel per channel, so a lookup here is a DB + Vault round
    trip per channel across a batch that can exceed a thousand."""
    client = MagicMock()
    client.get_channel_info.return_value = {"id": "C1", "name": "one"}
    dbcm, _cur = _db(fetchone_row=None)
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user") as probe, \
         patch.object(mod, "resolve_org", return_value="org"), \
         patch.object(mod, "set_rls_context", return_value="org"), \
         patch.object(mod.db_pool, "get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_upsert_channel", return_value=("C1", True)), \
         patch.object(mod, "get_redis_client", return_value=MagicMock()) as redis_get, \
         patch.object(mod, "_enqueue_metadata"):
        mod.register_single_channel("u1", "C1", team_id="T1")

    probe.assert_not_called()
    # Still invalidated, under the caller's workspace.
    redis_get.return_value.delete.assert_called_once_with("slack:available_channels:org:T1")


def test_activate_channels_skips_unjoinable():
    # A channel that can't be joined (e.g. private) is skipped, not registered.
    client = MagicMock()
    client.join_channel.side_effect = lambda cid: {"id": cid} if cid == "C1" else None
    registered = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "register_single_channel",
                      side_effect=lambda u, c, team_id=None: registered.append(c)):
        activated = mod._activate_channels("u1", ["C1", "C2_private"])

    assert activated == 1
    assert registered == ["C1"]


def test_activate_channels_registers_as_it_joins():
    # Each channel must be registered immediately after its own join, not in a
    # second pass after every join finishes. A large batch takes many minutes and
    # the manage page can only show channels that already have a row, so batching
    # the registrations to the end leaves the UI empty for the whole run (and
    # loses everything joined so far if the task is cancelled midway).
    client = MagicMock()
    client.join_channel.return_value = {"id": "ok"}
    calls = []
    with patch.object(mod, "get_slack_client_for_user", return_value=client), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "register_single_channel",
                      side_effect=lambda u, c, team_id=None: calls.append(("register", c))):
        client.join_channel.side_effect = lambda cid: calls.append(("join", cid)) or {"id": cid}
        activated = mod._activate_channels("u1", ["C1", "C2", "C3"])

    assert activated == 3
    assert calls == [
        ("join", "C1"), ("register", "C1"),
        ("join", "C2"), ("register", "C2"),
        ("join", "C3"), ("register", "C3"),
    ]


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


# --- workspace-listing cache ------------------------------------------------

def test_workspace_channels_cache_hit_skips_slack():
    # conversations.list is Tier 2 and pages ~8 requests on a large workspace, so
    # a cache hit must not call Slack at all — that's the whole point (one user
    # refreshing the page otherwise rate-limits the org's shared bot token).
    client = MagicMock()
    redis_client = MagicMock()
    redis_client.get.return_value = json.dumps([{"id": "C9", "name": "cached"}])
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "get_redis_client", return_value=redis_client), \
         patch.object(mod, "get_slack_client_for_user", return_value=client):
        result = mod._fetch_workspace_channels("u1")

    assert result == [{"id": "C9", "name": "cached"}]
    client.list_all_channels.assert_not_called()


def test_workspace_channels_cache_miss_fetches_and_stores():
    client = MagicMock()
    client.list_all_channels.return_value = [{"id": "C1", "name": "one", "is_private": False}]
    redis_client = MagicMock()
    redis_client.get.return_value = None  # cold cache
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "get_redis_client", return_value=redis_client), \
         patch.object(mod, "get_slack_client_for_user", return_value=client):
        result = mod._fetch_workspace_channels("u1")

    assert result == [{"id": "C1", "name": "one", "is_private": False}]
    client.list_all_channels.assert_called_once()
    # TTL so a stale entry expires rather than pinning the picker forever.
    key, ttl, _payload = redis_client.setex.call_args.args
    assert key == "slack:available_channels:org1:T1"
    assert ttl == mod.AVAILABLE_CHANNELS_CACHE_TTL


def test_workspace_channels_cache_is_scoped_per_workspace():
    """The listing is whatever the caller's token can see, and one org can connect
    several Slack workspaces — an org-only key would serve workspace A's channels
    to a user on workspace B."""
    keys = []
    redis_client = MagicMock()
    redis_client.get.return_value = None
    client = MagicMock()
    client.list_all_channels.return_value = [{"id": "C1", "name": "one"}]

    for team in ("T_ALPHA", "T_BETA"):
        with patch.object(mod, "resolve_org", return_value="org1"), \
             patch.object(mod, "_team_id_for_user", return_value=team), \
             patch.object(mod, "get_redis_client", return_value=redis_client), \
             patch.object(mod, "get_slack_client_for_user", return_value=client):
            mod._fetch_workspace_channels("u1")
        keys.append(redis_client.setex.call_args.args[0])

    assert keys == ["slack:available_channels:org1:T_ALPHA",
                    "slack:available_channels:org1:T_BETA"]


def test_workspace_channels_does_not_cache_without_a_known_workspace():
    """No workspace ID = no key that's provably safe to share, so go live to Slack
    rather than risk reading or writing another workspace's listing."""
    redis_client = MagicMock()
    client = MagicMock()
    client.list_all_channels.return_value = [{"id": "C1", "name": "one"}]
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value=None), \
         patch.object(mod, "get_redis_client", return_value=redis_client), \
         patch.object(mod, "get_slack_client_for_user", return_value=client):
        assert mod._fetch_workspace_channels("u1") == [{"id": "C1", "name": "one"}]

    redis_client.get.assert_not_called()
    redis_client.setex.assert_not_called()


def test_invalidate_available_channels_cache_targets_the_workspace_key():
    """Invalidation must clear the same key the read path uses, or a join/leave
    stays invisible until the TTL expires."""
    redis_client = MagicMock()
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "get_redis_client", return_value=redis_client):
        mod._invalidate_available_channels_cache("u1")

    redis_client.delete.assert_called_once_with("slack:available_channels:org1:T1")


def test_workspace_channels_survives_broken_redis():
    # A dead cache must degrade to a live Slack call, never break the page.
    client = MagicMock()
    client.list_all_channels.return_value = [{"id": "C1", "name": "one"}]
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "get_redis_client", side_effect=RuntimeError("redis down")), \
         patch.object(mod, "get_slack_client_for_user", return_value=client):
        assert mod._fetch_workspace_channels("u1") == [{"id": "C1", "name": "one"}]


def test_available_channels_filters_against_live_membership_on_cache_hit():
    # The cache holds the RAW listing, so membership is applied after the read —
    # otherwise a channel joined during the TTL would linger in the Inactive list.
    redis_client = MagicMock()
    redis_client.get.return_value = json.dumps([
        {"id": "C1", "name": "one"},
        {"id": "C2", "name": "two"},
    ])
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "get_redis_client", return_value=redis_client), \
         patch.object(mod, "get_slack_client_for_user", return_value=MagicMock()):
        available = mod._list_available_channels("u1", {"C1"})

    assert [c["channel_id"] for c in available] == ["C2"]


def test_workspace_channels_returns_none_when_slack_unreachable():
    # None (not []) so callers can tell "Slack down" from "no channels".
    client = MagicMock()
    client.list_all_channels.side_effect = RuntimeError("slack down")
    with patch.object(mod, "resolve_org", return_value="org1"), \
         patch.object(mod, "_team_id_for_user", return_value="T1"), \
         patch.object(mod, "get_redis_client", return_value=None), \
         patch.object(mod, "get_slack_client_for_user", return_value=client):
        assert mod._fetch_workspace_channels("u1") is None


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
         patch.object(mod, "_mark_pending", side_effect=lambda cur, cids: list(cids)), \
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

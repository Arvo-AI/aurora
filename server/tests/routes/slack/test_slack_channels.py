"""Unit tests for Slack channel classification and full-listing pagination."""

from unittest.mock import patch

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


# --- auto_register_channels: register all, describe only top-N --------------

def test_auto_register_describes_only_recency_cap():
    from unittest.mock import MagicMock

    # 3 channels; cap describe at 2. All 3 should be upserted, only 2 described.
    channels = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
        {"id": "C3", "name": "three", "is_member": False, "created": 100},
    ]

    fake_client = MagicMock()
    fake_client.list_all_channels.return_value = channels

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
        described = mod.auto_register_channels("11111111-1111-1111-1111-111111111111", describe_limit=2)

    # All 3 registered; only the 2 most-recent members described.
    assert {u[0] for u in upserts} == {"C1", "C2", "C3"}
    assert dict(upserts)["C1"] == "pending"
    assert dict(upserts)["C2"] == "pending"
    assert dict(upserts)["C3"] == "skipped"
    assert enqueued == ["C1", "C2"]
    assert described == 2


def test_auto_register_skips_dismissed_channels():
    from unittest.mock import MagicMock

    channels = [
        {"id": "C1", "name": "one", "is_member": True, "created": 300},
        {"id": "C2", "name": "two", "is_member": True, "created": 200},
    ]
    fake_client = MagicMock()
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

    # Stops once the cap is reached rather than looping forever.
    assert len(result) >= 300
    assert len(result) < 600

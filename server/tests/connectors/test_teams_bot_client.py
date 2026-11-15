"""Bot Framework client: service-URL allowlist, id validation and thread replies."""

import pytest

from connectors.teams_connector import bot_client as bc


@pytest.fixture(autouse=True)
def bot_env(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "secret")


@pytest.fixture
def posts(monkeypatch):
    sent = []

    def fake_post(service_url, path, body, *, timeout=30):
        sent.append({"service_url": service_url, "path": path, "body": body, "timeout": timeout})
        return {"id": "act-1"}

    monkeypatch.setattr(bc, "_post_activity", fake_post)
    return sent


# --- serviceUrl allowlist ----------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://smba.trafficmanager.net/teams/",
    "https://smba.trafficmanager.net/amer/",
    "https://directline.botframework.com/",
    "https://botframework.com/",
])
def test_allowed_service_urls(url):
    assert bc.is_allowed_service_url(url) is True


@pytest.mark.parametrize("url", [
    None,
    "",
    "http://smba.trafficmanager.net/teams/",            # not https
    "https://evil.example.com/",
    "https://smba.trafficmanager.net.evil.example.com/",  # suffix trick
    "https://evilbotframework.com/",                    # not a subdomain
    "https://smba.trafficmanager.net@evil.example.com/",  # userinfo trick
    "https://smba.trafficmanager.net:8443/",            # non-default port
    "https://user:pw@smba.trafficmanager.net/",
    "ftp://smba.trafficmanager.net/",
])
def test_rejected_service_urls(url):
    assert bc.is_allowed_service_url(url) is False


def test_default_service_url_is_allowed():
    assert bc._normalized_service_url(None) == "https://smba.trafficmanager.net/teams/"


def test_normalized_service_url_refuses_unknown_host():
    with pytest.raises(bc.TeamsBotError):
        bc._normalized_service_url("https://evil.example.com/")


# --- conversation / activity ids ----------------------------------------------

def test_thread_conversation_id_strips_existing_suffix():
    assert bc.thread_conversation_id("19:abc@thread.tacv2;messageid=1", "999") == "19:abc@thread.tacv2;messageid=999"


@pytest.mark.parametrize("bad", ["", "..", ".", "a/b", "a b", "a?x=1", "x" * 600])
def test_safe_conversation_id_rejects_bad_values(bad):
    with pytest.raises(bc.TeamsBotError):
        bc._safe_conversation_id(bad)


def test_safe_conversation_id_accepts_thread_form():
    value = "19:abc@thread.tacv2;messageid=1700000000000"
    assert bc._safe_conversation_id(value) == value


def test_reply_in_conversation_rejects_traversal(posts):
    with pytest.raises(bc.TeamsBotError):
        bc.send_reply_in_conversation(
            service_url="https://smba.trafficmanager.net/teams/", conversation_id="..", text="hi",
        )
    assert posts == []


# --- proactive posts ----------------------------------------------------------

def test_top_level_post_creates_a_conversation_without_reply_id(posts):
    bc.send_message_to_team_channel(
        tenant_id="tenant-1", team_id="team-1", channel_id="19:chan@thread.tacv2", text="hello",
    )
    assert posts[0]["path"] == "v3/conversations"
    assert posts[0]["body"]["channelData"]["channel"]["id"] == "19:chan@thread.tacv2"
    assert "replyToId" not in posts[0]["body"]["activity"]


def test_thread_reply_posts_into_the_root_messages_conversation(posts):
    bc.send_message_to_team_channel(
        tenant_id="tenant-1", team_id="team-1", channel_id="19:chan@thread.tacv2",
        text="follow-up", reply_to_id="1700000000000",
    )
    assert len(posts) == 1
    assert posts[0]["path"] == "v3/conversations/19:chan@thread.tacv2;messageid=1700000000000/activities"
    assert posts[0]["body"]["text"] == "follow-up"


def test_channel_id_with_thread_suffix_is_cleaned_before_use(posts):
    bc.send_message_to_team_channel(
        tenant_id="tenant-1", team_id="team-1", channel_id="19:chan@thread.tacv2;messageid=55", text="hi",
    )
    assert posts[0]["body"]["channelData"]["channel"]["id"] == "19:chan@thread.tacv2"


def test_post_refuses_a_forged_service_url(posts):
    with pytest.raises(bc.TeamsBotError):
        bc.send_message_to_team_channel(
            tenant_id="tenant-1", team_id="team-1", channel_id="19:chan@thread.tacv2",
            text="hi", service_url="https://evil.example.com/",
        )
    assert posts == []


# --- team_has_bot probe -----------------------------------------------------------

@pytest.mark.parametrize("status,expected", [(200, True), (403, False), (404, False), (401, None), (500, None)])
def test_team_has_bot_maps_connector_status(monkeypatch, status, expected):
    class Resp:
        status_code = status
        ok = status < 400

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        return Resp()

    monkeypatch.setattr(bc, "get_bot_access_token", lambda: "tok")
    monkeypatch.setattr(bc.requests, "get", fake_get)
    assert bc.team_has_bot("https://smba.trafficmanager.net/emea/", "19:abc@thread.tacv2") is expected
    # Uses the caller's regional host, not the default
    assert seen["url"] == "https://smba.trafficmanager.net/emea/v3/teams/19:abc@thread.tacv2/conversations"


def test_team_has_bot_refuses_a_foreign_host_and_bad_ids(monkeypatch):
    monkeypatch.setattr(bc, "get_bot_access_token", lambda: "tok")
    with pytest.raises(bc.TeamsBotError):
        bc.team_has_bot("https://evil.example.com/", "19:abc@thread.tacv2")
    with pytest.raises(bc.TeamsBotError):
        bc.team_has_bot(None, "../etc")

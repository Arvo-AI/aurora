"""Unit tests for description-driven Slack notification routing (no DB/network)."""

from unittest.mock import MagicMock, patch

from utils.notifications import slack_routing


# --- _parse_channel_ids -----------------------------------------------------

def test_parse_extracts_json_array_and_filters_invalid():
    valid = {"C1", "C2"}
    # Model wrapped the array in prose; C9 isn't a known channel.
    out = slack_routing._parse_channel_ids('Sure: ["C1","C9","C2"] done', valid)
    assert out == ["C1", "C2"]


def test_parse_dedupes_preserving_order():
    assert slack_routing._parse_channel_ids('["C2","C1","C2"]', {"C1", "C2"}) == ["C2", "C1"]


def test_parse_handles_garbage_and_empty():
    assert slack_routing._parse_channel_ids("no json here", {"C1"}) == []
    assert slack_routing._parse_channel_ids("[not valid json", {"C1"}) == []
    assert slack_routing._parse_channel_ids("[]", {"C1"}) == []


# --- resolve_notification_channels fallbacks --------------------------------

def test_no_candidates_falls_back_to_default():
    with patch.object(slack_routing, "_load_candidate_channels", return_value=[]):
        assert slack_routing.resolve_notification_channels("u1", {}, "C_DEFAULT") == ["C_DEFAULT"]


def test_llm_error_falls_back_to_default():
    candidates = [{"channel_id": "C1", "channel_name": "payments", "description": "payments team"}]
    with patch.object(slack_routing, "_load_candidate_channels", return_value=candidates), \
         patch("utils.hooks.get_hook", return_value=lambda *a, **k: (True, "")), \
         patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org1"), \
         patch("services.memory.slack_memory.read_slack_memory", return_value="policy"), \
         patch("chat.backend.agent.providers.create_chat_model", side_effect=RuntimeError("boom")):
        assert slack_routing.resolve_notification_channels("u1", {}, "C_DEFAULT") == ["C_DEFAULT"]


def test_hook_blocked_falls_back_to_default():
    candidates = [{"channel_id": "C1", "channel_name": "payments", "description": "payments team"}]
    with patch.object(slack_routing, "_load_candidate_channels", return_value=candidates), \
         patch("utils.hooks.get_hook", return_value=lambda *a, **k: (False, "limit reached")), \
         patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org1"):
        assert slack_routing.resolve_notification_channels("u1", {}, "C_DEFAULT") == ["C_DEFAULT"]


def test_llm_pick_returns_matched_channel():
    candidates = [
        {"channel_id": "C1", "channel_name": "payments", "description": "payments incidents"},
        {"channel_id": "C2", "channel_name": "general", "description": "chit chat"},
    ]
    fake_resp = MagicMock()
    fake_resp.content = '["C1"]'
    with patch.object(slack_routing, "_load_candidate_channels", return_value=candidates), \
         patch("utils.hooks.get_hook", return_value=lambda *a, **k: (True, "")), \
         patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org1"), \
         patch("services.memory.slack_memory.read_slack_memory", return_value="post payments incidents to #payments"), \
         patch("chat.backend.agent.providers.create_chat_model", return_value=MagicMock()), \
         patch("chat.backend.agent.utils.llm_usage_tracker.tracked_invoke", return_value=fake_resp):
        out = slack_routing.resolve_notification_channels(
            "u1", {"alert_title": "pay failed", "service": "payments", "severity": "high"}, "C_DEFAULT"
        )
    assert out == ["C1"]


def test_llm_empty_pick_falls_back_to_default():
    candidates = [{"channel_id": "C1", "channel_name": "payments", "description": "payments"}]
    fake_resp = MagicMock()
    fake_resp.content = "[]"  # model decided nothing fits
    with patch.object(slack_routing, "_load_candidate_channels", return_value=candidates), \
         patch("utils.hooks.get_hook", return_value=lambda *a, **k: (True, "")), \
         patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org1"), \
         patch("services.memory.slack_memory.read_slack_memory", return_value="policy"), \
         patch("chat.backend.agent.providers.create_chat_model", return_value=MagicMock()), \
         patch("chat.backend.agent.utils.llm_usage_tracker.tracked_invoke", return_value=fake_resp):
        assert slack_routing.resolve_notification_channels("u1", {}, "C_DEFAULT") == ["C_DEFAULT"]

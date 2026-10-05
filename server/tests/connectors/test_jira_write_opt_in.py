"""Jira writes are opt-in.

Atlassian 3LO has no bot principal, so a comment Aurora posts is authored by
the Atlassian account that connected the integration — writing has to be an
explicit org choice, and whatever gets posted has to say it came from Aurora.
"""

from __future__ import annotations

import json

import pytest

from connectors.jira_connector import attribution, settings
from connectors.jira_connector.adf_converter import adf_to_plain_text, markdown_to_adf


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def test_default_mode_is_read_only():
    assert settings.DEFAULT_MODE == settings.READ_ONLY


def test_unset_preference_resolves_to_read_only(monkeypatch):
    """An org that never opened the settings page must not get write access."""
    import utils.auth.stateless_auth as stateless_auth
    monkeypatch.setattr(
        stateless_auth, "get_user_preference",
        lambda user_id, key, default=None: default,
    )
    assert settings.get_jira_mode("uid-1") == settings.READ_ONLY


@pytest.mark.parametrize("stored", ["full", "FULL", " Full "])
def test_stored_mode_is_normalized(monkeypatch, stored):
    import utils.auth.stateless_auth as stateless_auth
    monkeypatch.setattr(
        stateless_auth, "get_user_preference",
        lambda user_id, key, default=None: stored,
    )
    assert settings.get_jira_mode("uid-1") == settings.FULL


@pytest.mark.parametrize("stored", ["", None, "bogus", "write", "comment"])
def test_unrecognised_mode_fails_closed(stored):
    """An unknown value must not be read as permission to post."""
    assert settings.normalize_jira_mode(stored) == settings.READ_ONLY
    assert settings.jira_writes_allowed(stored) is False


def test_writes_allowed_only_above_read_only():
    assert settings.jira_writes_allowed(settings.READ_ONLY) is False
    assert settings.jira_writes_allowed(settings.COMMENT_ONLY) is True
    assert settings.jira_writes_allowed(settings.FULL) is True


# ---------------------------------------------------------------------------
# Attribution banner
# ---------------------------------------------------------------------------

def test_banner_wraps_body_top_and_bottom():
    doc = attribution.attribute_adf(markdown_to_adf("## RCA\nroot cause here"))
    text = adf_to_plain_text(doc)
    assert text.startswith(attribution.HEADER)
    assert text.rstrip().endswith(attribution.FOOTER)
    assert "root cause here" in text


def test_banner_survives_the_data_center_plain_text_conversion():
    """Jira DC strips ADF marks; the wording must still be there."""
    doc = attribution.attribute_adf(markdown_to_adf("root cause here"))
    plain = adf_to_plain_text(doc)
    assert "did not write this" in plain


def test_banner_is_not_stacked_on_a_retry():
    once = attribution.attribute_adf(markdown_to_adf("root cause"))
    assert attribution.attribute_adf(once) == once


def test_banner_matches_despite_the_em_dash():
    """json.dumps escapes the header's em dash by default — the guard must not."""
    once = attribution.attribute_adf(markdown_to_adf("root cause"))
    assert "\\u2014" in json.dumps(once)
    assert attribution.attribute_adf(once) == once


def test_non_adf_payload_passes_through_untouched():
    assert attribution.attribute_adf({"not": "a doc"}) == {"not": "a doc"}
    assert attribution.attribute_adf(None) is None


# ---------------------------------------------------------------------------
# Agent tool refusals
# ---------------------------------------------------------------------------

@pytest.fixture
def jira_tool(monkeypatch):
    from chat.backend.agent.tools import jira_tool as mod

    # Any real client build would need credentials; a refusal must short-circuit
    # before that, so an exploding stub proves the gate ran first.
    def _explode(*_a, **_kw):
        raise AssertionError("reached Jira with writes disabled")

    monkeypatch.setattr(mod, "_get_client", _explode)
    return mod


def _mode(monkeypatch, jira_tool, mode):
    monkeypatch.setattr(jira_tool, "get_jira_mode", lambda user_id: mode)


@pytest.mark.parametrize(
    "call",
    [
        lambda m: m.jira_add_comment("PROJ-1", "findings", user_id="uid-1"),
        lambda m: m.jira_create_issue("PROJ", "summary", user_id="uid-1"),
        lambda m: m.jira_update_issue("PROJ-1", {"summary": "x"}, user_id="uid-1"),
        lambda m: m.jira_link_issues("PROJ-1", "PROJ-2", user_id="uid-1"),
    ],
)
def test_read_only_refuses_every_write_tool(monkeypatch, jira_tool, call):
    _mode(monkeypatch, jira_tool, settings.READ_ONLY)
    result = json.loads(call(jira_tool))
    assert result["status"] == "error"
    assert result["jira_mode"] == settings.READ_ONLY


@pytest.mark.parametrize(
    "call",
    [
        lambda m: m.jira_create_issue("PROJ", "summary", user_id="uid-1"),
        lambda m: m.jira_update_issue("PROJ-1", {"summary": "x"}, user_id="uid-1"),
        lambda m: m.jira_link_issues("PROJ-1", "PROJ-2", user_id="uid-1"),
    ],
)
def test_comment_only_refuses_create_update_and_link(monkeypatch, jira_tool, call):
    _mode(monkeypatch, jira_tool, settings.COMMENT_ONLY)
    result = json.loads(call(jira_tool))
    assert result["status"] == "error"
    assert result["jira_mode"] == settings.COMMENT_ONLY


def test_refusal_tells_the_agent_not_to_retry(monkeypatch, jira_tool):
    """A retry loop against a hard gate burns the whole RCA budget."""
    _mode(monkeypatch, jira_tool, settings.READ_ONLY)
    result = json.loads(jira_tool.jira_add_comment("PROJ-1", "findings", user_id="uid-1"))
    assert "do not retry" in result["error"].lower()


def test_comment_only_permits_commenting(monkeypatch, jira_tool):
    _mode(monkeypatch, jira_tool, settings.COMMENT_ONLY)
    posted = {}

    class _Client:
        base_url = "https://example.atlassian.net"

        def add_comment(self, issue_key, body_adf):
            posted["issue_key"] = issue_key
            posted["body"] = body_adf
            return {"id": "10001"}

    monkeypatch.setattr(jira_tool, "_get_client", lambda user_id: _Client())
    result = json.loads(jira_tool.jira_add_comment("PROJ-1", "root cause here", user_id="uid-1"))

    assert result["status"] == "success"
    assert posted["issue_key"] == "PROJ-1"
    # The comment carries Aurora's name even though Jira shows the user's.
    assert attribution.HEADER in adf_to_plain_text(posted["body"])

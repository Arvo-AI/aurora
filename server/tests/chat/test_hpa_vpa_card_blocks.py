"""Tests the Slack Block Kit payload for right-sizing recommendation cards.

Two properties here are load-bearing, and both fail in the same silent way -- an
over-limit or malformed field makes ``chat.postMessage`` reject the ENTIRE
message with ``invalid_blocks``, so the card does not post at all. There is no
partial render to degrade to, and the agent's run still reports success.

1. The Dismiss confirm dialog must stay inside Slack's field limits. Dismiss
   closes the PR *and* starts a cooldown, so the dialog is the only place a
   human is told that before it happens.
2. The post-dismiss rewrite must drop the Dismiss button (and its dialog)
   entirely, while keeping the View PR link when the status line asks someone to
   go close the PR by hand.

Pure functions only: no Slack, no DB, no network.
"""

import json
import os
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.tools.hpa_vpa_card_tool import (  # noqa: E402
    _CONFIRM_BUTTON_MAX,
    _CONFIRM_TEXT_MAX,
    _CONFIRM_TITLE_MAX,
    _clip,
    _dismiss_confirm,
    build_recommendation_blocks,
)

_REC = {
    "workload": "checkout-api",
    "service": "checkout-api",
    "environment": "production",
    "autoscaler": "HPA (CPU)",
    "pr_number": 18,
    "pr_url": "https://github.com/owner/repo/pull/18",
    "memory_current": "3 Gi", "memory_recommended": "832 Mi",
    "memory_evidence": "p95 627.1 Mi",
    "cpu_current": "1500 m", "cpu_recommended": "350 m",
    "cpu_evidence": "p95 247.0 m",
}


def _actions(blocks):
    return [b for b in blocks if b["type"] == "actions"]


def _buttons(blocks):
    rows = _actions(blocks)
    return [e["text"]["text"] for e in rows[0]["elements"]] if rows else []


def _dismiss_button(blocks):
    for row in _actions(blocks):
        for element in row["elements"]:
            if element["text"]["text"] == "Dismiss":
                return element
    return None


# ---------------------------------------------------------------------------
# Confirm dialog -- Slack field limits
# ---------------------------------------------------------------------------


def test_confirm_dialog_fits_slack_field_limits():
    """The reason this test exists: a 356-char confirm text failed the whole
    chat.postMessage with invalid_blocks, so no card posted at all. Slack caps
    text at 300, title at 100 and the buttons at 30."""
    confirm = _dismiss_confirm(18)

    assert len(confirm["title"]["text"]) <= _CONFIRM_TITLE_MAX
    assert len(confirm["text"]["text"]) <= _CONFIRM_TEXT_MAX
    assert len(confirm["confirm"]["text"]) <= _CONFIRM_BUTTON_MAX
    assert len(confirm["deny"]["text"]) <= _CONFIRM_BUTTON_MAX


def test_confirm_dialog_states_both_consequences():
    """"Dismiss" alone does not convey that the PR closes AND the workload goes
    quiet. Someone closing a duplicate would otherwise buy 30 days of silence on
    a workload that may still be mis-sized."""
    text = _dismiss_confirm(18)["text"]["text"]

    assert "#18" in text
    assert "30 days" in text
    # And it names the alternative that does not start a cooldown.
    assert "repo" in text.lower()


def test_confirm_dialog_has_the_required_slack_shape():
    confirm = _dismiss_confirm(18)

    assert confirm["title"]["type"] == "plain_text"
    assert confirm["confirm"]["type"] == "plain_text"
    assert confirm["deny"]["type"] == "plain_text"
    # Slack rejects mrkdwn in title/confirm/deny but requires a type on text.
    assert confirm["text"]["type"] in ("mrkdwn", "plain_text")


@pytest.mark.parametrize("limit", [_CONFIRM_TITLE_MAX, _CONFIRM_TEXT_MAX, _CONFIRM_BUTTON_MAX])
def test_clip_never_exceeds_the_limit(limit):
    assert len(_clip("x" * (limit + 200), limit)) == limit
    assert _clip("short", limit) == "short"


def test_clip_marks_that_it_truncated():
    assert _clip("y" * 500, 50).endswith("...")


# ---------------------------------------------------------------------------
# Button wiring
# ---------------------------------------------------------------------------


def test_dismiss_button_carries_the_confirm_dialog():
    button = _dismiss_button(build_recommendation_blocks(_REC, "rec-1"))

    assert button is not None
    assert "confirm" in button
    assert button["style"] == "danger"
    # The bare uuid must still travel in `value` -- the handler parses it.
    assert button["value"] == "rec-1"


def test_view_pr_button_has_no_confirm():
    """It is a link-out. A dialog on it would be friction with no consequence."""
    blocks = build_recommendation_blocks(_REC, "rec-1")
    view = [e for e in _actions(blocks)[0]["elements"] if e["text"]["text"].startswith("View PR")][0]

    assert "confirm" not in view
    assert view["url"] == _REC["pr_url"]


def test_post_dismiss_rewrite_drops_dismiss_and_its_dialog():
    """No button means no re-click -- the strongest idempotency guarantee, layered
    on the conditional UPDATE in dismiss_recommendation."""
    blocks = build_recommendation_blocks(
        _REC, "rec-1", with_actions=False, status_line="*Status:* Dismissed"
    )

    assert _actions(blocks) == []
    assert _dismiss_button(blocks) is None


def test_manual_work_card_keeps_view_pr_but_not_dismiss():
    """When the status line asks a human to close the PR themselves, deleting the
    only link to that PR would be actively unhelpful."""
    blocks = build_recommendation_blocks(
        _REC, "rec-1", with_actions=False, link_only=True,
        status_line="*Status:* could not be closed automatically",
    )

    assert _buttons(blocks) == ["View PR #18"]
    assert _dismiss_button(blocks) is None


def test_missing_pr_url_drops_only_the_link_button():
    """Slack rejects a url-less link button and fails the whole call. Dropping the
    whole row would take Dismiss with it and leave a 'proposed' row nobody can
    retire, blocking the workload behind the partial unique index."""
    rec = dict(_REC, pr_url=None)
    blocks = build_recommendation_blocks(rec, "rec-1")

    assert _buttons(blocks) == ["Dismiss"]
    assert _dismiss_button(blocks) is not None


def test_blocks_are_json_serializable():
    """chat.postMessage sends these as JSON; a non-serializable value fails the
    whole call rather than one field."""
    json.dumps(build_recommendation_blocks(_REC, "rec-1"))


# ---------------------------------------------------------------------------
# Cooldown compensation on an unexpected failure
# ---------------------------------------------------------------------------


def test_unexpected_failure_restores_a_superseded_cooldown(monkeypatch):
    """The supersede is COMMITTED before the card is posted, so a raise anywhere
    after it -- a failing claim, a dropped connection -- exits past the per-path
    compensation in _post_new and strands the row as 'superseded'. That silently
    destroys the human's remaining anti-nag window, so the outer handler has to
    compensate too.

    Verified against a live Postgres separately; this pins the wiring so the
    handler cannot lose its restore call in a later refactor.
    """
    import chat.backend.agent.tools.hpa_vpa_card_tool as tool

    restored = []
    monkeypatch.setattr(tool, "_resolve_slack_target",
                        lambda _uid: {"client": object(), "channel_id": "C1"})
    monkeypatch.setattr(tool, "_restore_cooldown_standalone",
                        lambda uid, sid, wl: restored.append(sid))

    # Fail once the cooldown has already been superseded.
    class _Boom(Exception):
        pass

    def _explode(*_a, **_k):
        raise _Boom("connection lost after the supersede committed")

    monkeypatch.setattr(tool.db_pool, "get_admin_connection", _explode)

    result = json.loads(tool.send_hpa_vpa_recommendation(
        user_id="u1", workload="checkout-api", repo="owner/repo", pr_number=18,
        pr_url="https://github.com/owner/repo/pull/18",
        cpu_current="1500 m", cpu_recommended="350 m",
    ))

    # The tool still fails soft rather than raising into the agent loop...
    assert "error" in result
    # ...and the compensation hook is always reached, even when the failure
    # happened before any superseded_id could be assigned (None is a no-op).
    assert restored == [None]

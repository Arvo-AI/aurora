"""Started/completed/failed Slack notifications: thread placement per the layer-3 matrix.

Patches the Slack client factory and channel lookup on the service; the DB is
routed to fake_pool by patched_db and routes.slack.slack_events_helpers is
stubbed by the package conftest.
"""

from datetime import datetime

import pytest

from utils.notifications import slack_notification_service as svc

from utils.notifications.slack_threading import RECURRENCE_FOOTER_BLOCK_ID
from .slack_fakes import ANCHOR_ID, ANCHOR_TS, CHAN, CHILD_ID, CHILD_TS, FIRST_POSTED_TS


@pytest.fixture
def run(monkeypatch, patched_db, slack_helpers_stub):
    def _run(incident_data, client, *, kind="completed", error_message=None):
        monkeypatch.setattr(svc, "get_slack_client_for_user", lambda user_id: client)
        monkeypatch.setattr(svc, "_get_incidents_channel_id", lambda user_id, c: CHAN)
        if kind == "failed":
            return svc.send_slack_investigation_failed_notification("u1", incident_data, error_message=error_message)
        if kind == "started":
            return svc.send_slack_investigation_started_notification("u1", incident_data)
        return svc.send_slack_investigation_completed_notification("u1", incident_data)
    return _run


def _header(blocks):
    return [b["text"]["text"] for b in (blocks or []) if b["type"] == "header"]


def _section_text(blocks):
    return "\n".join(b["text"]["text"] for b in (blocks or []) if b["type"] == "section" and "text" in b)


def _updated_card(client, ts):
    cards = [u for u in client.updated if u["ts"] == ts]
    assert len(cards) == 1, cards
    return cards[0]


def _null_cas(updates):
    return [(sql, params) for sql, params in updates if "SET slack_message_ts = NULL" in sql]


class TestStandalone:
    def test_updates_own_started_message_in_place(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(), client) is True
        assert client.sent == []
        assert len(client.updated) == 1
        assert client.updated[0]["ts"] == ANCHOR_TS
        assert _header(client.updated[0]["blocks"]) == ["Analysis Complete"]
        assert client.deleted == []
        assert patched_db.updates == []

    def test_update_rejected_threads_under_own_started_message(self, run, make_client, patched_db, standalone):
        # cant_update_message: the Started message is ours but not editable — reply in its thread.
        client = make_client(fail_update=True)
        assert run(standalone(), client) is True
        assert client.updated == []
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert _header(client.sent[0]["blocks"]) == ["Analysis Complete"]
        assert client.deleted == []
        assert patched_db.updates == []

    def test_without_ts_posts_top_level_and_backfills(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(slack_message_ts=None), client) is True
        assert client.sent[0]["thread_ts"] is None
        assert patched_db.updates == [(patched_db.updates[0][0], (FIRST_POSTED_TS, ANCHOR_ID, None))]
        assert "IS NOT DISTINCT FROM" in patched_db.updates[0][0]

    def test_started_message_gone_posts_top_level_and_replaces_parent(self, run, make_client, patched_db, standalone):
        # Slack ignored the stale thread_ts and the lookup confirms the parent is gone:
        # the card is top-level and becomes the new thread parent.
        client = make_client(missing={ANCHOR_TS})
        assert run(standalone(), client) is True
        assert len(client.sent) == 1
        assert client.deleted == []
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, ANCHOR_TS)

    def test_thread_rejection_retries_top_level_but_keeps_live_parent(self, run, make_client, patched_db, standalone):
        # Update rejected, then cannot_reply_to_message: the card goes top-level, but the
        # Started message still exists, so it stays the thread parent / @mention key.
        client = make_client(fail_update=True, fail_thread=True)
        assert run(standalone(), client) is True
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] is None
        assert _header(client.sent[0]["blocks"]) == ["Analysis Complete"]
        assert patched_db.updates == []

    def test_other_rejection_is_a_single_attempt(self, run, make_client, patched_db, standalone):
        # not_in_channel fails the same way for update and post: no retry as a post.
        client = make_client(fail_all=True, reject_with="not_in_channel")
        assert run(standalone(), client) is False
        assert client.update_attempts + client.attempts == 1
        assert patched_db.updates == []

    def test_transport_error_is_a_single_attempt(self, run, make_client, patched_db, standalone):
        client = make_client(transport_error=True)
        assert run(standalone(), client) is False
        assert client.sent == []
        assert patched_db.updates == []

    def test_group_root_card_keeps_recurrence_footer(self, run, make_client, patched_db, standalone):
        # Children folded in before the anchor completed: rebuilding its card keeps the footer.
        client = make_client()
        assert run(standalone(group_size=3), client) is True
        card = client.updated[0]
        assert _header(card["blocks"]) == ["Analysis Complete"]
        assert card["blocks"][-1]["block_id"] == RECURRENCE_FOOTER_BLOCK_ID
        assert "3 occurrences" in card["blocks"][-1]["elements"][0]["text"]

    def test_webhook_title_is_escaped_in_full_card(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(alert_title="<!channel> down", service="<a|b>"), client) is True
        card = client.updated[0]
        section = _section_text(card["blocks"])
        assert "<!channel>" not in section
        assert "&lt;!channel&gt; down" in section
        assert "&lt;a|b&gt;" in section
        assert "<!channel>" not in card["text"]

class TestFoldedChild:
    def test_compact_reply_under_anchor_and_retire_started(self, run, make_client, patched_db, folded,
                                                            slack_helpers_stub):
        client = make_client()
        assert run(folded(), client) is True
        assert len(client.sent) == 1
        msg = client.sent[0]
        assert msg["thread_ts"] == ANCHOR_TS
        assert _header(msg["blocks"]) == []
        assert "occurrence 2 of 3" in _section_text(msg["blocks"])
        assert "Root Cause Analysis" not in _section_text(msg["blocks"])
        assert msg["blocks"][-1]["type"] == "context"
        assert f"/incidents/{ANCHOR_ID}|" in msg["blocks"][-1]["elements"][0]["text"]
        assert client.deleted == [(CHAN, CHILD_TS)]
        assert len(patched_db.updates) == 1
        sql, params = patched_db.updates[0]
        assert "SET slack_message_ts = NULL" in sql
        assert "recurrence_of_incident_id = %s" in sql
        assert params == (CHILD_ID, CHILD_TS, ANCHOR_ID)
        slack_helpers_stub.get_incident_suggestions.assert_not_called()

    def test_child_without_own_ts_skips_retire(self, run, make_client, patched_db, folded):
        client = make_client()
        assert run(folded(slack_message_ts=None), client) is True
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert client.deleted == []
        assert patched_db.updates == []

    def test_started_message_with_replies_is_kept_and_gets_a_pointer(self, run, make_client, patched_db, folded):
        client = make_client(replies={CHILD_TS: 1})
        assert run(folded(), client) is True
        assert len(client.sent) == 2
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        pointer = client.sent[1]
        assert pointer["thread_ts"] == CHILD_TS
        assert "Folded into" in pointer["text"]
        assert f"/incidents/{ANCHOR_ID}|" in pointer["text"]
        assert client.deleted == []
        assert patched_db.updates == []
        # the kept card itself no longer reads "In Progress": it is edited into a pointer stub
        stub = _updated_card(client, CHILD_TS)
        assert _header(stub["blocks"]) == []
        assert f"Folded into <{svc._get_incident_url(ANCHOR_ID)}|" in _section_text(stub["blocks"])
        assert "occurrence 2 of 3" in _section_text(stub["blocks"])
        assert stub["blocks"][0]["accessory"]["url"].endswith(CHILD_ID)

    def test_compact_reply_marks_anchor_card_with_recurrence_footer(self, run, make_client, patched_db, folded):
        client = make_client()
        assert run(folded(), client) is True
        card = _updated_card(client, ANCHOR_TS)
        assert _header(card["blocks"]) == ["Investigation Started"]  # existing blocks preserved
        footer = card["blocks"][-1]
        assert footer["block_id"] == RECURRENCE_FOOTER_BLOCK_ID
        assert "3 occurrences" in footer["elements"][0]["text"]
        assert card["text"] == "Investigation Started: High CPU"

    def test_recurrence_footer_is_replaced_not_stacked(self, run, make_client, patched_db, folded):
        old_footer = {"type": "context", "block_id": RECURRENCE_FOOTER_BLOCK_ID,
                      "elements": [{"type": "mrkdwn", "text": "2 occurrences"}]}
        header = {"type": "header", "block_id": "hdr", "text": {"type": "plain_text", "text": "Analysis Complete"}}
        client = make_client(cards={ANCHOR_TS: {"blocks": [header, old_footer], "text": "Analysis Complete: High CPU"}})
        assert run(folded(), client) is True
        card = _updated_card(client, ANCHOR_TS)
        footers = [b for b in card["blocks"] if b.get("block_id") == RECURRENCE_FOOTER_BLOCK_ID]
        assert len(footers) == 1
        assert "3 occurrences" in footers[0]["elements"][0]["text"]
        assert _header(card["blocks"]) == ["Analysis Complete"]

    def test_recurrence_footer_skips_plain_text_anchor(self, run, make_client, patched_db, folded):
        client = make_client(cards={ANCHOR_TS: {"blocks": [{"type": "rich_text", "elements": []}], "text": "plain"}})
        assert run(folded(), client) is True
        assert [u for u in client.updated if u["ts"] == ANCHOR_TS] == []
        assert client.deleted == [(CHAN, CHILD_TS)]

    def test_recurrence_footer_failure_does_not_fail_the_notification(self, run, make_client, patched_db, folded):
        client = make_client(fail_update=True)
        assert run(folded(), client) is True
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert client.updated == []
        assert client.deleted == [(CHAN, CHILD_TS)]

    def test_anchor_without_ts_updates_own_started_in_place(self, run, make_client, patched_db, folded):
        client = make_client()
        assert run(folded(anchor_slack_message_ts=None), client) is True
        assert client.sent == []
        assert client.updated[0]["ts"] == CHILD_TS
        assert _header(client.updated[0]["blocks"]) == ["Analysis Complete"]
        assert client.deleted == []
        assert patched_db.updates == []  # own message kept: nothing to seed

    def test_anchor_without_ts_and_top_level_card_is_own_parent(self, run, make_client, patched_db, folded):
        # The group has no thread parent: the child's top-level card becomes the
        # CHILD's thread parent, never the anchor's — slack_message_ts also routes
        # @mentions, and a card showing this incident must resolve to it.
        client = make_client()
        assert run(folded(anchor_slack_message_ts=None, slack_message_ts=None), client) is True
        assert client.sent[0]["thread_ts"] is None
        assert _header(client.sent[0]["blocks"]) == ["Analysis Complete"]
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, CHILD_ID, None)
        assert "IS NOT DISTINCT FROM" in patched_db.updates[0][0]

    def test_thread_rejection_degrades_to_full_top_level_card(self, run, make_client, patched_db, folded):
        client = make_client(fail_thread=True, fail_update=True)
        assert run(folded(), client) is True
        # compact attempt and own-thread attempt both rejected (not recorded), the
        # in-place update too; full card went top-level
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] is None
        assert _header(client.sent[0]["blocks"]) == ["Analysis Complete"]
        assert client.deleted == []
        assert patched_db.updates == []  # the anchor still has a parent: nothing to seed

    def test_anchor_lookup_is_reused_for_the_footer(self, run, make_client, patched_db, folded):
        client = make_client()
        assert run(folded(), client) is True
        assert client.lookups.count(ANCHOR_TS) == 1
        assert _updated_card(client, ANCHOR_TS)["blocks"][-1]["block_id"] == RECURRENCE_FOOTER_BLOCK_ID

    def test_kept_replies_pointer_failure_does_not_fail_the_notification(self, run, make_client, patched_db, folded):
        # The compact reply landed; the pointer into the kept Started thread is best effort.
        client = make_client(replies={CHILD_TS: 1})
        real_send = client.send_message

        def flaky_send(*args, **kwargs):
            if client.attempts >= 1:
                raise ValueError("Failed to communicate with Slack: read timeout")
            return real_send(*args, **kwargs)

        client.send_message = flaky_send
        assert run(folded(), client) is True
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] == ANCHOR_TS

    def test_anchor_parent_gone_is_forgotten_and_card_updates_own_started(self, run, make_client, patched_db, folded):
        # One lookup, no stray post: the anchor's stale ts is cleared so later siblings
        # skip straight to their own message, which is updated in place.
        client = make_client(missing={ANCHOR_TS})
        assert run(folded(), client) is True
        assert client.sent == []
        assert client.updated[0]["ts"] == CHILD_TS
        assert _header(client.updated[0]["blocks"]) == ["Analysis Complete"]
        assert client.deleted == []
        assert _null_cas(patched_db.updates) == [(patched_db.updates[0][0], (ANCHOR_ID, ANCHOR_TS))]
        assert len(patched_db.updates) == 1

    def test_anchor_parent_vanishing_after_lookup_removes_stray(self, run, make_client, patched_db, folded):
        # Race: the parent was present at lookup time, Slack still placed the reply top-level.
        client = make_client(ignore_thread=True)
        assert run(folded(), client) is True
        assert len(client.sent) == 1
        assert "occurrence 2 of 3" in _section_text(client.sent[0]["blocks"])
        assert client.updated[0]["ts"] == CHILD_TS
        assert _header(client.updated[0]["blocks"]) == ["Analysis Complete"]
        # only the stray compact post is removed; the child's Started message is kept (and updated)
        assert client.deleted == [(CHAN, FIRST_POSTED_TS)]
        assert patched_db.updates == []

    def test_stray_that_cannot_be_removed_keeps_child_started(self, run, make_client, patched_db, folded):
        # The stray compact card is visible, so no second card — but it is not a
        # thread reply, so the child's Started message and ts must survive.
        client = make_client(ignore_thread=True, fail_delete=True)
        assert run(folded(), client) is True
        assert len(client.sent) == 1
        assert client.deleted == []
        assert patched_db.updates == []

    def test_transport_error_on_compact_reply_is_a_single_attempt(self, run, make_client, patched_db, folded):
        client = make_client(transport_error=True)
        assert run(folded(), client) is False
        assert client.sent == []
        assert client.deleted == []
        assert patched_db.updates == []

    def test_delete_failure_still_releases_ts(self, run, make_client, patched_db, folded):
        client = make_client(fail_delete=True)
        assert run(folded(), client) is True
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert client.deleted == []
        assert [p for _, p in _null_cas(patched_db.updates)] == [(CHILD_ID, CHILD_TS, ANCHOR_ID)]


class TestPlainTextFallback:
    def test_invalid_blocks_update_in_place_as_plain_text(self, run, make_client, patched_db, standalone, slack_helpers_stub):
        slack_helpers_stub.validate_slack_blocks.return_value = False
        client = make_client()
        assert run(standalone(), client) is True
        assert client.sent == []
        card = client.updated[0]
        assert card["ts"] == ANCHOR_TS
        assert card["blocks"] == []  # old Started blocks cleared, text shows
        assert "Analysis Complete" in card["text"]
        assert patched_db.updates == []

    def test_invalid_blocks_keep_thread_placement_when_update_rejected(self, run, make_client, patched_db, standalone,
                                                                        slack_helpers_stub):
        slack_helpers_stub.validate_slack_blocks.return_value = False
        client = make_client(fail_update=True)
        assert run(standalone(), client) is True
        assert len(client.sent) == 1
        assert client.sent[0]["blocks"] is None
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert "Analysis Complete" in client.sent[0]["text"]
        assert patched_db.updates == []

    def test_invalid_blocks_without_ts_still_backfills(self, run, make_client, patched_db, standalone,
                                                       slack_helpers_stub):
        slack_helpers_stub.validate_slack_blocks.return_value = False
        client = make_client()
        assert run(standalone(slack_message_ts=None), client) is True
        assert client.sent[0]["thread_ts"] is None
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, None)

    def test_folded_invalid_reply_blocks_still_threads_as_text(self, run, make_client, patched_db, folded,
                                                               slack_helpers_stub):
        slack_helpers_stub.validate_slack_blocks.return_value = False
        client = make_client()
        assert run(folded(), client) is True
        assert client.sent[0]["blocks"] is None
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert "occurrence 2 of 3" in client.sent[0]["text"]
        assert client.deleted == [(CHAN, CHILD_TS)]


class TestFailed:
    def test_standalone_updates_own_started_in_place(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(), client, kind="failed", error_message="boom <here>") is True
        assert client.sent == []
        assert client.updated[0]["ts"] == ANCHOR_TS
        assert _header(client.updated[0]["blocks"]) == ["Investigation Failed"]
        section = _section_text(client.updated[0]["blocks"])
        assert "boom &lt;here&gt;" in section
        assert "<here>" not in section
        assert patched_db.updates == []

    def test_does_not_replace_the_card_of_a_completed_investigation(self, run, make_client, patched_db, standalone):
        # A follow-up chat (or the stall sweep) failing after the RCA completed: the
        # Complete card stays; the Failed card goes into its thread.
        client = make_client()
        assert run(standalone(analyzed_at=datetime(2026, 9, 8, 12, 0)), client, kind="failed") is True
        assert client.updated == []
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert _header(client.sent[0]["blocks"]) == ["Investigation Failed"]
        assert patched_db.updates == []

    def test_standalone_without_ts_backfills(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(slack_message_ts=None), client, kind="failed") is True
        assert client.sent[0]["thread_ts"] is None
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, None)

    def test_folded_compact_failed_reply(self, run, make_client, patched_db, folded):
        client = make_client()
        assert run(folded(), client, kind="failed", error_message="boom") is True
        msg = client.sent[0]
        assert msg["thread_ts"] == ANCHOR_TS
        assert _header(msg["blocks"]) == []
        text = _section_text(msg["blocks"])
        assert "occurrence 2 of 3" in text
        assert ":x: *Error:* boom" in text
        assert client.deleted == [(CHAN, CHILD_TS)]

    def test_folded_anchor_without_ts_updates_own_started(self, run, make_client, patched_db, folded):
        client = make_client()
        assert run(folded(anchor_slack_message_ts=None), client, kind="failed") is True
        assert client.sent == []
        assert client.updated[0]["ts"] == CHILD_TS
        assert _header(client.updated[0]["blocks"]) == ["Investigation Failed"]
        assert client.deleted == []
        assert patched_db.updates == []

class TestStarted:
    def test_top_level_and_becomes_thread_parent(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(slack_message_ts=None), client, kind="started") is True
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] is None
        assert _header(client.sent[0]["blocks"]) == ["Investigation Started"]
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, None)
        assert "IS NOT DISTINCT FROM" in patched_db.updates[0][0]

    def test_joins_existing_thread_without_overwriting_parent(self, run, make_client, patched_db, standalone):
        # e.g. the stall sweep already posted a Failed card that became the parent.
        client = make_client()
        assert run(standalone(), client, kind="started") is True
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert patched_db.updates == []

    def test_stale_parent_is_replaced_only_when_gone(self, run, make_client, patched_db, standalone):
        client = make_client(missing={ANCHOR_TS})
        assert run(standalone(), client, kind="started") is True
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, ANCHOR_TS)

    def test_webhook_title_is_escaped(self, run, make_client, patched_db, standalone):
        client = make_client()
        assert run(standalone(slack_message_ts=None, alert_title="<!channel> down"), client, kind="started") is True
        section = _section_text(client.sent[0]["blocks"])
        assert "<!channel>" not in section
        assert "&lt;!channel&gt; down" in section

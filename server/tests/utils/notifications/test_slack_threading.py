"""slack_threading helpers: compact reply, post fallback, Started-message retirement, DB writes."""

import html
import json

import pytest

from connectors.slack_connector.client import SlackAPIError
from utils.notifications import slack_threading as st

from .slack_fakes import ANCHOR_ID, ANCHOR_TS, CHAN, CHILD_ID, CHILD_TS, FIRST_POSTED_TS


class TestIsFoldedChild:
    def test_reads_recurrence_of(self, folded, standalone):
        assert st.is_folded_child(folded()) is True
        assert st.is_folded_child(standalone()) is False


class TestBuildRecurrenceReply:
    def test_completed_reply_shape(self, folded):
        text, blocks = st.build_recurrence_reply(
            folded(), incident_url="http://f/incidents/" + CHILD_ID, anchor_url="http://f/incidents/" + ANCHOR_ID)
        json.dumps(blocks)
        section = blocks[0]
        assert section["type"] == "section"
        assert "occurrence 2 of 3" in section["text"]["text"]
        assert "*Alert:* High CPU" in section["text"]["text"]
        assert len(section["text"]["text"]) < 3000
        assert section["accessory"]["url"] == "http://f/incidents/" + CHILD_ID
        assert section["accessory"]["text"]["text"] == "View Occurrence"
        assert blocks[-1]["type"] == "context"
        assert f"<http://f/incidents/{ANCHOR_ID}|High CPU>" in blocks[-1]["elements"][0]["text"]
        assert "occurrence 2 of 3" in text
        assert CHILD_ID in text
        assert not any(b["type"] == "header" for b in blocks)

    def test_anchor_title_is_escaped_and_capped(self, folded):
        long_title = "a<b>&c" * 40
        _, blocks = st.build_recurrence_reply(
            folded(anchor_alert_title=long_title), incident_url="u", anchor_url="http://a")
        ctx = blocks[-1]["elements"][0]["text"]
        assert "<b>" not in ctx.split("|", 1)[1]
        assert "&lt;b&gt;&amp;c" in ctx
        label = ctx.split("|", 1)[1].rstrip(">")
        assert label == html.escape(long_title[:st._ANCHOR_TITLE_MAX], quote=False) + "…"

    def test_child_fields_are_escaped(self, folded):
        text, blocks = st.build_recurrence_reply(
            folded(alert_title="<!channel> down", service="<a|b>"), incident_url="u", anchor_url="a",
            error_text="boom <here>")
        section = blocks[0]["text"]["text"]
        assert "<!channel>" not in section
        assert "&lt;!channel&gt; down" in section
        assert "&lt;a|b&gt;" in section
        assert "&lt;here&gt;" in blocks[1]["text"]["text"]
        assert "<!channel>" not in text

    def test_failed_variant_has_error_line(self, folded):
        text, blocks = st.build_recurrence_reply(folded(), incident_url="u", anchor_url="a", error_text="boom")
        assert blocks[1]["type"] == "section"
        assert ":x: *Error:* boom" in blocks[1]["text"]["text"]
        assert blocks[-1]["type"] == "context"
        assert text.startswith("Investigation failed — ")

    def test_no_error_text_means_no_error_line(self, folded):
        _, blocks = st.build_recurrence_reply(folded(), incident_url="u", anchor_url="a")
        assert [b["type"] for b in blocks] == ["section", "context"]


class TestBuildFoldPointer:
    def test_links_anchor_with_escaped_title(self, folded):
        text = st.build_fold_pointer(folded(anchor_alert_title="<!channel> CPU"), anchor_url="http://a")
        assert "<http://a|&lt;!channel&gt; CPU>" in text
        assert "<!channel>" not in text


class TestMessageIsGone:
    def test_missing_is_gone(self, make_client):
        assert st.message_is_gone(make_client(missing={ANCHOR_TS}), channel=CHAN, ts=ANCHOR_TS) is True

    def test_present_is_not_gone(self, make_client):
        assert st.message_is_gone(make_client(), channel=CHAN, ts=ANCHOR_TS) is False

    def test_lookup_failure_counts_as_present(self, make_client):
        client = make_client()
        client.get_message = lambda channel, ts: (_ for _ in ()).throw(ValueError("Slack API error: ratelimited"))
        assert st.message_is_gone(client, channel=CHAN, ts=ANCHOR_TS) is False


class TestTryPostThreaded:
    def test_forwards_thread_ts(self, make_client):
        client = make_client()
        result = st.try_post_threaded(client, channel=CHAN, text="t", blocks=[{"type": "divider"}], thread_ts=ANCHOR_TS)
        assert result["ts"]
        assert client.sent == [{"channel": CHAN, "text": "t", "thread_ts": ANCHOR_TS, "blocks": [{"type": "divider"}]}]

    def test_thread_rejection_returns_none_single_call(self, make_client):
        client = make_client(fail_thread=True)
        assert st.try_post_threaded(client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS) is None
        assert client.sent == []
        assert client.attempts == 1

    @pytest.mark.parametrize("code", ["not_in_channel", "invalid_auth", "msg_too_long", "invalid_blocks"])
    def test_other_rejection_propagates(self, make_client, code):
        # Would fail identically top-level: no degrade, no second call.
        client = make_client(fail_thread=True, reject_with=code)
        with pytest.raises(SlackAPIError) as exc:
            st.try_post_threaded(client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS)
        assert exc.value.error == code
        assert client.attempts == 1

    def test_transport_error_propagates(self, make_client):
        # The post may have landed: never swallow, so the caller cannot double-post.
        client = make_client(transport_error=True)
        with pytest.raises(ValueError):
            st.try_post_threaded(client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS)

    def test_require_threaded_removes_stray_top_level_post(self, make_client):
        # Slack silently posts top-level when the parent is missing/deleted.
        client = make_client(ignore_thread=True)
        result = st.try_post_threaded(
            client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS, require_threaded=True)
        assert result is None
        assert len(client.sent) == 1
        assert client.deleted == [(CHAN, FIRST_POSTED_TS)]  # the stray, never the parent

    def test_require_threaded_accepts_real_reply(self, make_client):
        client = make_client()
        result = st.try_post_threaded(
            client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS, require_threaded=True)
        assert result["message"]["thread_ts"] == ANCHOR_TS
        assert client.deleted == []

    def test_lenient_keeps_top_level_placement(self, make_client):
        client = make_client(ignore_thread=True)
        result = st.try_post_threaded(client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS)
        assert result is not None
        assert st.placed_thread_ts(result) is None
        assert client.deleted == []

    def test_require_threaded_stray_delete_failure_returns_top_level_result(self, make_client):
        # The stray is already visible: keep it, and let the caller see it was not threaded.
        client = make_client(ignore_thread=True, fail_delete=True)
        result = st.try_post_threaded(
            client, channel=CHAN, text="t", blocks=None, thread_ts=ANCHOR_TS, require_threaded=True)
        assert result is not None
        assert st.placed_thread_ts(result) is None
        assert len(client.sent) == 1


class TestPostWithThreadFallback:
    def test_threaded_ok_is_one_call(self, make_client):
        client = make_client()
        result = st.post_with_thread_fallback(client, channel=CHAN, text="t", thread_ts=ANCHOR_TS)
        assert len(client.sent) == 1
        assert client.sent[0]["thread_ts"] == ANCHOR_TS
        assert st.placed_thread_ts(result) == ANCHOR_TS

    def test_thread_rejection_retries_top_level(self, make_client):
        client = make_client(fail_thread=True)
        result = st.post_with_thread_fallback(client, channel=CHAN, text="t", blocks=[{"type": "divider"}], thread_ts=ANCHOR_TS)
        assert result["ts"]
        assert len(client.sent) == 1  # the threaded attempt was rejected before recording
        assert client.attempts == 2
        assert client.sent[0]["thread_ts"] is None
        assert client.sent[0]["blocks"] == [{"type": "divider"}]

    def test_other_rejection_is_single_attempt(self, make_client):
        client = make_client(fail_thread=True, reject_with="not_in_channel")
        with pytest.raises(SlackAPIError):
            st.post_with_thread_fallback(client, channel=CHAN, text="t", thread_ts=ANCHOR_TS)
        assert client.attempts == 1

    def test_transport_error_is_not_retried(self, make_client):
        client = make_client(transport_error=True)
        with pytest.raises(ValueError):
            st.post_with_thread_fallback(client, channel=CHAN, text="t", thread_ts=ANCHOR_TS)
        assert client.sent == []

    def test_no_thread_ts_is_plain_post(self, make_client):
        client = make_client()
        st.post_with_thread_fallback(client, channel=CHAN, text="t")
        assert client.sent[0]["thread_ts"] is None

    def test_top_level_failure_propagates(self, make_client):
        client = make_client(fail_all=True)
        with pytest.raises(ValueError):
            st.post_with_thread_fallback(client, channel=CHAN, text="t", thread_ts=ANCHOR_TS)


class TestClearChildStartedMessage:
    def test_releases_ts_then_deletes(self, make_client, patched_db, folded):
        client = make_client()
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.RELEASED
        assert client.deleted == [(CHAN, CHILD_TS)]
        sql, params = patched_db.updates[0]
        assert "SET slack_message_ts = NULL" in sql
        assert "AND slack_message_ts = %s" in sql
        assert "AND recurrence_of_incident_id = %s" in sql  # only while the fold still holds
        assert params == (CHILD_ID, CHILD_TS, ANCHOR_ID)
        assert patched_db.conn.commit.called

    def test_message_with_replies_is_kept(self, make_client, patched_db, folded):
        # A human thread already hangs off the Started message: keep it and its routing key.
        client = make_client(replies={CHILD_TS: 2})
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.KEPT_REPLIES
        assert client.deleted == []
        assert patched_db.updates == []

    def test_already_gone_message_only_clears_ts(self, make_client, patched_db, folded):
        client = make_client(missing={CHILD_TS})
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.RELEASED
        assert client.deleted == []
        assert len(patched_db.updates) == 1
        assert patched_db.updates[0][1] == (CHILD_ID, CHILD_TS, ANCHOR_ID)

    def test_lookup_failure_leaves_everything(self, make_client, patched_db, folded):
        client = make_client()
        client.get_message = lambda channel, ts: (_ for _ in ()).throw(ValueError("Slack API error: ratelimited"))
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.KEPT
        assert client.deleted == []
        assert patched_db.updates == []

    def test_fold_no_longer_holding_keeps_message(self, make_client, patched_db, folded):
        # Layer 1 unfolded this incident concurrently (mutual-fold tie-break): the CAS
        # matches nothing, so the Started message and its ts survive.
        client = make_client()
        patched_db.cursor.rowcount = 0
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.KEPT
        assert client.deleted == []
        assert len(patched_db.updates) == 1

    def test_delete_failure_after_release(self, make_client, patched_db, folded):
        client = make_client(fail_delete=True)
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.RELEASED
        assert client.deleted == []
        assert len(patched_db.updates) == 1

    def test_no_ts_is_noop(self, make_client, patched_db, folded):
        client = make_client()
        outcome = st.clear_child_started_message(
            client, channel=CHAN, user_id="u1", incident_data=folded(slack_message_ts=None))
        assert outcome == st.KEPT
        assert client.deleted == []
        assert patched_db.executes == []

    def test_db_failure_skips_delete(self, make_client, patched_db, folded):
        client = make_client()
        patched_db.cursor.execute.side_effect = RuntimeError("db down")
        outcome = st.clear_child_started_message(client, channel=CHAN, user_id="u1", incident_data=folded())
        assert outcome == st.KEPT
        assert client.deleted == []


class TestClearAnchorMessageTs:
    def test_compare_and_set_to_null(self, patched_db):
        st.clear_anchor_message_ts("u1", ANCHOR_ID, ANCHOR_TS)
        sql, params = patched_db.updates[0]
        assert "SET slack_message_ts = NULL" in sql
        assert "AND slack_message_ts = %s" in sql
        assert params == (ANCHOR_ID, ANCHOR_TS)


class TestBackfillThreadParentTs:
    def test_update_is_compare_and_set_on_null(self, patched_db):
        assert st.backfill_thread_parent_ts("u1", ANCHOR_ID, ANCHOR_TS) is True
        sql, params = patched_db.updates[0]
        assert "SET slack_message_ts = %s" in sql
        assert "slack_message_ts IS NOT DISTINCT FROM %s" in sql
        assert params == (ANCHOR_TS, ANCHOR_ID, None)
        assert patched_db.conn.commit.called

    def test_replacing_a_stale_ts(self, patched_db):
        st.backfill_thread_parent_ts("u1", ANCHOR_ID, "1700000000.000009", replacing=ANCHOR_TS)
        assert patched_db.updates[0][1] == ("1700000000.000009", ANCHOR_ID, ANCHOR_TS)

    def test_db_error_swallowed(self, patched_db):
        patched_db.cursor.execute.side_effect = RuntimeError("db down")
        assert st.backfill_thread_parent_ts("u1", ANCHOR_ID, ANCHOR_TS) is False

    def test_empty_ts_is_noop(self, patched_db):
        assert st.backfill_thread_parent_ts("u1", ANCHOR_ID, "") is False
        assert patched_db.executes == []


class TestRecordThreadParent:
    def _post(self, client, thread_ts=None):
        return client.send_message(CHAN, "t", thread_ts=thread_ts)

    def test_threaded_post_is_not_a_parent(self, make_client, patched_db):
        client = make_client()
        result = self._post(client, ANCHOR_TS)
        assert st.record_thread_parent(client, channel=CHAN, user_id="u1", incident_id=ANCHOR_ID,
                                       result=result, stored=ANCHOR_TS) is False
        assert patched_db.executes == []

    def test_top_level_without_stored_parent_is_recorded(self, make_client, patched_db):
        client = make_client()
        result = self._post(client)
        assert st.record_thread_parent(client, channel=CHAN, user_id="u1", incident_id=ANCHOR_ID,
                                       result=result, stored=None) is True
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, None)

    def test_live_stored_parent_is_never_replaced(self, make_client, patched_db):
        # Landed top-level (thread rejection / other channel) but the Started message still exists.
        client = make_client(ignore_thread=True)
        result = self._post(client, ANCHOR_TS)
        assert st.record_thread_parent(client, channel=CHAN, user_id="u1", incident_id=ANCHOR_ID,
                                       result=result, stored=ANCHOR_TS) is False
        assert patched_db.updates == []

    def test_gone_stored_parent_is_replaced(self, make_client, patched_db):
        client = make_client(missing={ANCHOR_TS})
        result = self._post(client, ANCHOR_TS)
        assert st.record_thread_parent(client, channel=CHAN, user_id="u1", incident_id=ANCHOR_ID,
                                       result=result, stored=ANCHOR_TS) is True
        assert patched_db.updates[0][1] == (FIRST_POSTED_TS, ANCHOR_ID, ANCHOR_TS)

    def test_lookup_failure_keeps_stored_parent(self, make_client, patched_db):
        client = make_client(ignore_thread=True)
        result = self._post(client, ANCHOR_TS)
        client.get_message = lambda channel, ts: (_ for _ in ()).throw(ValueError("Slack API error: ratelimited"))
        assert st.record_thread_parent(client, channel=CHAN, user_id="u1", incident_id=ANCHOR_ID,
                                       result=result, stored=ANCHOR_TS) is False
        assert patched_db.updates == []

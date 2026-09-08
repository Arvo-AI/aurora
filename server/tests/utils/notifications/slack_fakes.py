"""Fakes and incident_data builders shared by the Slack thread-consolidation tests."""

import uuid
from unittest.mock import MagicMock

from connectors.slack_connector.client import SlackAPIError

ANCHOR_ID = str(uuid.uuid4())
CHILD_ID = str(uuid.uuid4())
ANCHOR_TS = "1700000000.000001"
CHILD_TS = "1700000000.000002"
# ts values the fake hands out for posts use a different epoch from the stored
# constants above, so an assertion on "which ts" can tell them apart.
FIRST_POSTED_TS = "1800000000.000001"
SECOND_POSTED_TS = "1800000000.000002"
CHAN = "C123"


def folded_incident(**over):
    """CHILD_ID folded into ANCHOR_ID; both Started messages stored."""
    data = {
        'incident_id': CHILD_ID, 'alert_title': 'High CPU', 'severity': 'critical', 'service': 'api',
        'source_type': 'datadog', 'aurora_summary': 'What happened.\n\nRoot cause: disk full.',
        'slack_message_ts': CHILD_TS, 'recurrence_of': ANCHOR_ID, 'anchor_slack_message_ts': ANCHOR_TS,
        'anchor_alert_title': 'High CPU', 'occurrence_number': 2, 'group_size': 3,
    }
    data.update(over)
    return data


def standalone_incident(**over):
    """ANCHOR_ID on its own, with its Started message stored."""
    data = folded_incident(
        incident_id=ANCHOR_ID, slack_message_ts=ANCHOR_TS, recurrence_of=None,
        anchor_slack_message_ts=None, anchor_alert_title=None, occurrence_number=1, group_size=1,
    )
    data.update(over)
    return data


class FakeSlackClient:
    """Records chat.postMessage / chat.delete and answers message lookups from
    its options; raises SlackAPIError the way SlackClient does on ok=false."""

    def __init__(self, *, fail_thread=False, fail_delete=False, fail_all=False, transport_error=False,
                 ignore_thread=False, missing=(), replies=None, reject_with="cannot_reply_to_message"):
        self.fail_thread = fail_thread  # ok=false on any threaded post
        self.fail_delete = fail_delete
        self.fail_all = fail_all  # ok=false on any post
        self.reject_with = reject_with  # the ok=false error code for fail_thread / fail_all
        self.transport_error = transport_error  # network failure: SlackClient wraps it in a plain ValueError
        self.ignore_thread = ignore_thread  # parent vanished after the lookup (race): replies land top-level
        self.missing = set(missing)  # ts values Slack no longer has: lookups return None, replies land top-level
        self.replies = dict(replies or {})  # ts -> reply_count of stored thread parents
        self.sent = []
        self.deleted = []
        self.attempts = 0  # every chat.postMessage call, including rejected ones
        self._n = 0

    def send_message(self, channel, text, thread_ts=None, blocks=None):
        self.attempts += 1
        if self.transport_error:
            raise ValueError("Failed to communicate with Slack: read timeout")
        if self.fail_all or (self.fail_thread and thread_ts):
            raise SlackAPIError(self.reject_with)
        self._n += 1
        self.sent.append({"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks})
        ts = f"1800000000.{self._n:06d}"
        message = {"ts": ts}
        if thread_ts and not self.ignore_thread and thread_ts not in self.missing:
            message["thread_ts"] = thread_ts
        return {"ok": True, "channel": channel, "ts": ts, "message": message}

    def delete_message(self, channel, ts):
        if self.fail_delete:
            raise SlackAPIError("cant_delete_message")
        self.deleted.append((channel, ts))

    def get_message(self, channel, ts):
        if self.transport_error:
            raise ValueError("Failed to communicate with Slack: read timeout")
        if ts in self.missing:
            return None
        message = {"ts": ts}
        if self.replies.get(ts):
            message["reply_count"] = self.replies[ts]
        return message


class FakePool:
    """db_pool stand-in: one MagicMock connection; every execute is recorded
    and reports one changed row unless a test sets cursor.rowcount."""

    def __init__(self):
        self.conn = MagicMock(name="conn")
        self.cursor = MagicMock(name="cursor")
        self.cursor.fetchone.return_value = ("owner@example.com",)
        self.cursor.rowcount = 1
        self.conn.cursor.return_value.__enter__.return_value = self.cursor
        self.pool = MagicMock(name="db_pool")
        self.pool.get_admin_connection.return_value.__enter__.return_value = self.conn

    @property
    def executes(self):
        return [(" ".join(c.args[0].split()), c.args[1] if len(c.args) > 1 else None)
                for c in self.cursor.execute.call_args_list]

    @property
    def updates(self):
        return [(sql, params) for sql, params in self.executes if sql.startswith("UPDATE incidents")]

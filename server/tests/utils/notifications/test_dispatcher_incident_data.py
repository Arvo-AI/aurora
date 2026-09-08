"""dispatcher._get_incident_data: recurrence-group fields for Slack threading."""

import uuid
from datetime import datetime

from utils.notifications import dispatcher

A = str(uuid.uuid4())
B = str(uuid.uuid4())
_T0 = datetime(2026, 9, 1, 12, 0, 0)

_EXISTING_KEYS = (
    'incident_id', 'user_id', 'source_type', 'status', 'severity', 'alert_title',
    'service', 'aurora_status', 'aurora_summary', 'started_at', 'analyzed_at',
    'created_at', 'slack_message_ts', 'google_chat_message_name',
)


def _row(incident_id, *, recurrence_of=None, anchor_ts=None, anchor_title=None, n=1, size=1):
    return (
        uuid.UUID(incident_id), "u1", "datadog", "investigating", "critical", "High CPU",
        "api", "completed", "summary text", _T0, _T0, _T0, "1700000000.000100", None,
        uuid.UUID(recurrence_of) if recurrence_of else None, anchor_ts, anchor_title, n, size,
    )


def _fetch(row, incident_id, fake_pool):
    fake_pool.cursor.fetchone.return_value = row
    return dispatcher._get_incident_data(incident_id, "u1")


def test_folded_row_populates_group_fields(patched_db):
    fake_pool = patched_db
    data = _fetch(_row(B, recurrence_of=A, anchor_ts="1700000000.000001", anchor_title="High CPU", n=2, size=3), B, fake_pool)
    assert data['recurrence_of'] == A
    assert data['anchor_slack_message_ts'] == "1700000000.000001"
    assert data['anchor_alert_title'] == "High CPU"
    assert data['occurrence_number'] == 2
    assert data['group_size'] == 3
    sql, params = fake_pool.executes[0]
    assert params == (B, B)
    assert "ROW_NUMBER() OVER" in sql
    assert "LEFT JOIN incidents anchor" in sql
    assert "JOIN grp ON grp.id = i.id" in sql  # the group always contains the incident itself
    assert "g.id = me.root_id OR g.recurrence_of_incident_id = me.root_id" in sql


def test_standalone_row_keeps_existing_keys(patched_db):
    fake_pool = patched_db
    data = _fetch(_row(A), A, fake_pool)
    assert data['recurrence_of'] is None
    assert data['anchor_slack_message_ts'] is None
    assert data['occurrence_number'] == 1
    assert data['group_size'] == 1
    for key in _EXISTING_KEYS:
        assert key in data
    assert data['incident_id'] == A
    assert data['slack_message_ts'] == "1700000000.000100"
    assert data['service'] == "api"


def test_no_row_returns_none(patched_db, caplog):
    fake_pool = patched_db
    assert _fetch(None, A, fake_pool) is None
    assert not [r for r in caplog.records if r.exc_info]

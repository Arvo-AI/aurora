"""Exercise SNS state transitions against the disposable alert-pipeline database."""
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv("ALERT_PIPELINE_INTEGRATION") != "1",
                                reason="requires the disposable alert-pipeline database")


@pytest.fixture
def intake():
    from routes.aws import cloudwatch_tasks
    from utils.db.connection_pool import db_pool

    assert os.environ["POSTGRES_DB"] == "aurora_test"
    user, org = str(uuid4()), str(uuid4())

    def sql(query, params):
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall() if cursor.description else []
            conn.commit()
            return rows

    sql("INSERT INTO organizations(id, name, slug) VALUES (%s, 'SNS QA', %s)", (org, org))
    sql("INSERT INTO users(id, email, password_hash, org_id) VALUES (%s, %s, 'unused', %s)",
        (user, user + "@example.test", org))

    def send(body, second, topic="test-topic", message_id=None):
        cloudwatch_tasks._process_alarm_in_db(
            user, {"Type": "Notification", "Message": body},
            {"topic_arn": topic, "published_at": f"2026-01-01T12:00:{second:02d}Z",
             "sns_message_id": message_id or str(uuid4())}, skip_rca=False,
        )

    def statuses():
        return sql("SELECT status FROM incidents WHERE org_id=%s ORDER BY source_alert_id", (org,))

    with patch.object(cloudwatch_tasks, "_post_incident_actions") as actions, \
            patch.object(cloudwatch_tasks, "_try_correlate_alarm", return_value=False):
        yield send, statuses, actions, sql, org


def test_recovery_requires_same_device_topic_and_later_publish_time(intake):
    send, statuses, actions, sql, org = intake
    send("Terminal kiosk-7 went down", 10)
    send("Terminal kiosk-7 went up", 9)
    send("Terminal kiosk-8 is online", 11)
    send("Terminal kiosk-7 went up", 12, topic="other-topic")
    send("Test", 13)
    send("Routine notification", 14)
    assert statuses() == [("investigating",)]
    send("Terminal KIOSK-7 went up", 15)
    send("Terminal kiosk-7 is online", 16)
    # The original RCA may finish after the recovery arrives.
    from chat.background.task import _update_incident_status_and_aurora
    [(incident, owner)] = sql("SELECT id,user_id FROM incidents WHERE org_id=%s", (org,))
    _update_incident_status_and_aurora(str(incident), "analyzed", "summarizing", owner)
    assert sql("SELECT aurora_status FROM incidents WHERE id=%s", (incident,)) == [("summarizing",)]
    assert statuses() == [("resolved",)]
    assert actions.call_count == 1
    assert sql("SELECT count(*) FROM cloudwatch_alarms WHERE org_id=%s", (org,)) == [(8,)]
    assert sql("SELECT count(*) FROM incident_lifecycle_events WHERE org_id=%s AND new_value='resolved'",
               (org,)) == [(1,)]


def test_repeated_offline_delivery_updates_existing_incident_even_concurrently(intake):
    send, statuses, actions, sql, org = intake
    ongoing = "RPT: kiosk-8\nClient: example site\nConnectivity: OFFLINE → OFFLINE\nLow cash: $2"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda second: send(ongoing, second), (2, 3)))
    send("Terminal kiosk-8 went up", 4)
    assert statuses() == [("investigating",)]
    assert actions.call_count == 1
    assert sql("SELECT count(*) FROM incident_alerts WHERE org_id=%s AND correlation_strategy='continued'",
               (org,)) == [(1,)]


def test_recovery_after_continued_offline_or_multiple_open_incidents(intake):
    send, statuses, actions, _, _ = intake
    down = "Terminal kiosk-7 is down"
    up = "Terminal kiosk-7 is online"
    send(down, 1)
    send("Device: kiosk-7\nConnectivity: OFFLINE -> OFFLINE", 5)
    send(up, 4)
    assert statuses() == [("investigating",)]
    send(up, 6)
    assert statuses() == [("resolved",)]
    send(down, 7)
    send(down, 8)
    send(up, 9)
    assert statuses() == [("resolved",), ("investigating",), ("investigating",)]
    assert actions.call_count == 3


def test_recovery_leaves_incident_with_another_correlated_problem_open(intake):
    send, statuses, actions, sql, org = intake
    send("Terminal kiosk-7 went down", 1)
    sql("""
        INSERT INTO incident_alerts (user_id, org_id, incident_id, source_type, source_alert_id, alert_title)
        SELECT user_id, org_id, id, 'sentry', 7, 'Payment processing failed' FROM incidents WHERE org_id=%s
        """, (org,))
    send("Terminal kiosk-7 went up", 2)
    assert statuses() == [("investigating",)]
    assert actions.call_count == 1


def test_failed_rca_cleanup_keeps_a_recovered_incident_resolved(intake):
    from chat.background import task
    send, _, _, sql, org = intake
    send("Device kiosk-9 went down", 1)
    send("Device kiosk-9 is online", 2)
    [(user,)] = sql("UPDATE incidents SET aurora_status='running', updated_at=NOW()-interval '1 hour' "
                    "WHERE org_id=%s RETURNING user_id", (org,))
    with patch.object(task, "_affected_users", return_value=[user]):
        assert task.cleanup_orphaned_investigations() == 1
    assert sql("SELECT status,aurora_status FROM incidents WHERE org_id=%s", (org,)) == [("resolved", "error")]

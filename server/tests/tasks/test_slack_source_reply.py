from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from requests import Response

from connectors.slack_connector.client import SlackAPIError
from tasks import slack_source_reply as worker
from utils.notifications import dispatcher


@pytest.fixture
def delivery(monkeypatch):
    incident = str(uuid4())
    data = {"incident_id": incident, "created_at": 1000, "alert_metadata": {},
            "aurora_status": "complete", "aurora_summary": "The sensor lost power.",
            "source_type": "sentry"}
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(worker.db_pool, "get_admin_connection", lambda: conn)
    conn.__enter__.return_value = conn
    monkeypatch.setattr(worker, "set_rls_context", lambda *args: "org")
    monkeypatch.setattr(worker, "get_org_preference", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "get_credentials_from_db", lambda *args: {"access_token": "test", "team_id": "T"})
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda *args: data)
    # Missing match defaults to a completed, error-free scan of the bot's channels.
    lookup = Mock(return_value=None)
    monkeypatch.setattr(worker, "_find_target", lookup)

    def run(attempts=0, errors=0, target=None, now=1001):
        monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: now))
        cur.fetchone.side_effect = [(True,), (attempts, errors, target)]
        worker.deliver_source_reply.run("user", incident)
        reply, target_json, failures, delay, _, _ = cur.execute.call_args.args[1]
        return reply, failures, delay

    return run, lookup


@pytest.mark.parametrize("now,attempts,errors,expected_delay", [
    (1001, 0, 0, 30), (1179, 1, 0, 1), (1180, 1, 0, None), (1600, 0, 0, None),
    (1001, 8, 0, 179), (1001, 9, 0, None), (1001, 9, 3, 30),
])
def test_missing_parent_final_scan_and_lookup_ceiling(delivery, now, attempts, errors, expected_delay):
    run, lookup = delivery
    assert run(attempts, errors, now=now) == (None, errors, expected_delay)
    lookup.assert_called_once()


@pytest.mark.parametrize("code", ["missing_scope", "invalid_auth", "token_revoked", "restricted_action_thread_locked"])
def test_permanent_rejection_stops_on_first_attempt(delivery, code):
    run, lookup = delivery
    lookup.side_effect = SlackAPIError(code)
    assert run() == (None, 1, None)


@pytest.mark.parametrize("errors,expected_delay", [(0, 240), (3, 240), (4, None)])
def test_slack_rate_limit_is_deferred_once_per_attempt(delivery, monkeypatch, errors, expected_delay):
    run, lookup = delivery
    lookup.side_effect = lambda cur, client, *args: client.list_bot_channels()
    response = Response()
    response.status_code = 429
    response.headers["Retry-After"] = "240"
    request = Mock(return_value=response)
    monkeypatch.setattr("connectors.slack_connector.client.requests.get", request)
    assert run(attempts=errors + 2, errors=errors) == (None, errors + 1, expected_delay)
    request.assert_called_once()


def test_incomplete_search_retries_after_window_closes_then_stops_on_fifth_failure(delivery):
    run, lookup = delivery
    lookup.side_effect = ValueError("Incomplete Slack history page")
    for errors, delay in enumerate([30, 60, 120, 240, None]):
        assert run(attempts=errors + 1, errors=errors, now=1300) == (None, errors + 1, delay)


def test_cleanup_rate_limit_retries_only_the_delete(delivery, monkeypatch):
    run, lookup = delivery
    lookup.return_value = {"channel": "C", "ts": "1000", "team": "T"}
    post = Mock(return_value={"ts": "1001", "message": {}})
    monkeypatch.setattr("utils.notifications.slack_notification_service.send_source_rca_reply", post)
    response = Response()
    response.status_code = 429
    response.headers["Retry-After"] = "180"
    request = Mock(return_value=response)
    monkeypatch.setattr("connectors.slack_connector.client.requests.post", request)
    assert run() == (None, 1, 180)
    assert lookup.return_value["unthreaded_ts"] == "1001"
    assert run(attempts=1, errors=1, target=lookup.return_value) == (None, 2, 180)
    assert request.call_count == 2
    post.assert_called_once()
    lookup.assert_called_once()

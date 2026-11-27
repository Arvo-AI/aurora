"""Each Slack event runs once, however many times Slack delivers it.

Slack redelivers an event it did not get a 200 for within 3s (immediately,
then ~1 min and ~5 min later). The handler claims the event_id in Redis before
any side effect, so a redelivery does nothing. Redis is faked: no I/O.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask

from routes.slack import slack_events, slack_events_helpers
from services.slack import socket_mode

TEAM = "T0TEAM0001"
SLACK_USER = "U0USER0001"


class FakeRedis:
    """Just enough of redis-py for SET NX EX and EXISTS."""

    def __init__(self):
        self.store: dict = {}
        self.ttls: dict = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    def exists(self, key):
        return int(key in self.store)


class DownRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def exists(self, *args, **kwargs):
        raise ConnectionError("redis down")


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(slack_events_helpers, "get_redis_client", lambda: redis)
    return redis


@pytest.fixture
def runs(monkeypatch):
    """Counts the side effect of an app_home_opened event (one per real run)."""
    task = MagicMock()
    monkeypatch.setattr(slack_events, "publish_home_task", task)
    return task.delay


def _event(event_id="Ev0EVENT0001"):
    return {
        "type": "event_callback",
        "event_id": event_id,
        "team_id": TEAM,
        "event": {"type": "app_home_opened", "tab": "home", "user": SLACK_USER},
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(slack_events, "verify_slack_signature", lambda *a: True)
    app = Flask(__name__)  # NOSONAR
    app.register_blueprint(slack_events.slack_events_bp, url_prefix="/slack")
    return app.test_client()


def _post(client, payload, retry_num=None):
    headers = {"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=x"}
    if retry_num is not None:
        headers["X-Slack-Retry-Num"] = str(retry_num)
        headers["X-Slack-Retry-Reason"] = "http_timeout"
    return client.post("/slack/events", json=payload, headers=headers)


def test_same_event_delivered_twice_runs_once(fake_redis, runs):
    slack_events.process_event_callback(_event())
    slack_events.process_event_callback(_event())
    assert runs.call_count == 1


def test_claim_expires_after_an_hour(fake_redis, runs):
    slack_events.process_event_callback(_event("Ev0TTL"))
    assert fake_redis.ttls["slack:event:Ev0TTL"] == 3600


def test_different_events_each_run(fake_redis, runs):
    slack_events.process_event_callback(_event("Ev0A"))
    slack_events.process_event_callback(_event("Ev0B"))
    assert runs.call_count == 2


def test_event_without_an_id_still_runs(fake_redis, runs):
    payload = _event()
    del payload["event_id"]
    slack_events.process_event_callback(payload)
    assert runs.call_count == 1


def test_redis_down_still_processes(monkeypatch, runs):
    monkeypatch.setattr(slack_events_helpers, "get_redis_client", lambda: DownRedis())
    slack_events.process_event_callback(_event())
    assert runs.call_count == 1


def test_redis_unavailable_still_processes(monkeypatch, runs):
    monkeypatch.setattr(slack_events_helpers, "get_redis_client", lambda: None)
    slack_events.process_event_callback(_event())
    assert runs.call_count == 1


def test_http_retry_of_a_claimed_event_acks_without_processing(fake_redis, client, monkeypatch):
    fake_redis.set("slack:event:Ev0EVENT0001", "1", nx=True, ex=3600)
    process = MagicMock()
    monkeypatch.setattr(slack_events, "process_event_callback", process)

    resp = _post(client, _event(), retry_num=1)

    assert resp.status_code == 200
    process.assert_not_called()


def test_http_retry_of_an_unseen_event_is_processed(fake_redis, client, runs):
    resp = _post(client, _event(), retry_num=2)
    assert resp.status_code == 200
    assert runs.call_count == 1


def test_http_delivery_then_retries_run_once(fake_redis, client, runs):
    for retry_num in (None, 1, 2, 3):
        assert _post(client, _event(), retry_num=retry_num).status_code == 200
    assert runs.call_count == 1


def test_http_retry_with_redis_down_is_processed(monkeypatch, client, runs):
    monkeypatch.setattr(slack_events_helpers, "get_redis_client", lambda: DownRedis())
    resp = _post(client, _event(), retry_num=1)
    assert resp.status_code == 200
    assert runs.call_count == 1


def test_bad_signature_never_claims_the_event(fake_redis, client, monkeypatch, runs):
    monkeypatch.setattr(slack_events, "verify_slack_signature", lambda *a: False)
    assert _post(client, _event()).status_code == 403
    assert fake_redis.store == {}
    assert runs.call_count == 0


def test_socket_mode_redelivery_runs_once(fake_redis, runs, monkeypatch):
    monkeypatch.setattr(socket_mode, "_load_handlers", lambda: {
        "SocketModeResponse": MagicMock(),
        "process_event_callback": slack_events.process_event_callback,
        "process_interaction": MagicMock(),
    })
    ws = MagicMock()
    first = SimpleNamespace(type="events_api", envelope_id="env-1", payload=_event(),
                            retry_attempt=0, retry_reason="")
    retry = SimpleNamespace(type="events_api", envelope_id="env-2", payload=_event(),
                            retry_attempt=1, retry_reason="timeout")

    socket_mode._dispatch(ws, first)
    socket_mode._dispatch(ws, retry)

    assert runs.call_count == 1
    assert ws.send_socket_mode_response.call_count == 2

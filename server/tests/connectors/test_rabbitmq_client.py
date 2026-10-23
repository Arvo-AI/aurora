"""RabbitMQ management client: URL guard, response slimming, and safe errors."""

import json
import socket
from unittest.mock import MagicMock

import pytest

from connectors.rabbitmq_connector import client as rmq


def _response(status, payload):
    body = json.dumps(payload).encode()
    response = MagicMock()
    response.status_code = status
    response.ok = 200 <= status < 300
    response.content = body
    response.json.return_value = payload
    return response


def _client(session, url="http://10.1.2.3:15672"):
    return rmq.RabbitMQClient(
        url=url,
        username="monitor",
        password="s3cret-password",
        session=session,
    )


def test_normalize_accepts_private_management_url():
    assert rmq.normalize_management_url("http://10.1.2.3:15672/") == "http://10.1.2.3:15672"
    assert rmq.normalize_management_url("https://[fd00::10]:15672") == "https://[fd00::10]:15672"


@pytest.mark.parametrize("raw", [
    "",
    "ftp://10.1.2.3",
    "http://user:pw@10.1.2.3:15672",
    "http://10.1.2.3:15672/api/overview",
    "http://10.1.2.3:15672?x=1",
    "http://127.0.0.1:15672",
    "http://169.254.169.254",
    "http://[::1]:15672",
    "http://[::ffff:127.0.0.1]:15672",
    "http://[::ffff:169.254.169.254]/",
    "http://0.0.0.0:15672",
    "http://224.0.0.1:15672",
    "https://metadata.google.internal",
])
def test_normalize_rejects_unsafe_urls(raw):
    with pytest.raises(ValueError):
        rmq.normalize_management_url(raw)


def test_normalize_rejects_hostname_that_resolves_to_metadata(monkeypatch):
    def _addrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", port or 0))]

    monkeypatch.setattr(rmq.socket, "getaddrinfo", _addrinfo)
    with pytest.raises(ValueError):
        rmq.normalize_management_url("https://broker.example.com:15672")


def test_normalize_accepts_hostname_on_a_private_address(monkeypatch):
    def _addrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.8.0.4", port or 0))]

    monkeypatch.setattr(rmq.socket, "getaddrinfo", _addrinfo)
    assert rmq.normalize_management_url("https://broker.example.com:15672") == "https://broker.example.com:15672"


def test_path_segment_encodes_dot_dot():
    assert rmq.path_segment("../overview") == "%2E%2E%2Foverview"
    assert rmq.path_segment("/") == "%2F"


def test_auth_failure_does_not_echo_secrets():
    session = MagicMock()
    session.request.return_value = _response(401, {"error": "s3cret-password leaked"})
    broker = _client(session)
    with pytest.raises(rmq.RabbitMQAPIError) as caught:
        broker.whoami()
    message = str(caught.value)
    assert "s3cret-password" not in message
    assert "leaked" not in message
    assert session.request.call_args.kwargs["allow_redirects"] is False
    assert session.trust_env is False
    assert session.request.call_args.kwargs["timeout"] == rmq.TIMEOUT_SECONDS
    assert "s3cret-password" not in session.request.call_args.args[1]


def test_redirect_is_not_followed():
    session = MagicMock()
    session.request.return_value = _response(302, {"location": "http://169.254.169.254/"})
    broker = _client(session)
    with pytest.raises(rmq.RabbitMQAPIError):
        broker.overview()


def test_pagination_fallback_retries_without_page_params():
    session = MagicMock()
    session.request.side_effect = [
        _response(400, {"error": "bad pagination"}),
        _response(200, [{"name": "jobs", "vhost": "/", "messages": 3, "arguments": {}}]),
    ]
    page = _client(session).queues()
    assert page["total_count"] == 1
    assert page["items"][0]["name"] == "jobs"
    second_params = session.request.call_args_list[1].kwargs["params"]
    assert "pagination" not in second_params


def test_queue_summary_keeps_dead_letter_args_and_drops_samples():
    summary = rmq.summarize_queue({
        "name": "jobs",
        "vhost": "/",
        "messages": 4,
        "messages_ready": 4,
        "messages_unacknowledged": 0,
        "consumers": 0,
        "arguments": {
            "x-dead-letter-exchange": "dlx",
            "x-custom-secret": "do-not-keep",
        },
        "message_stats": {
            "publish": 10,
            "publish_details": {"rate": 1.5, "samples": [1, 2, 3]},
        },
    })
    assert summary["arguments"] == {"x-dead-letter-exchange": "dlx"}
    assert summary["rates"]["publish"] == {"count": 10, "rate": 1.5}
    assert "samples" not in json.dumps(summary)


def test_health_checks_keep_failure_reasons_and_mark_missing_checks():
    session = MagicMock()

    def _respond(method, url, **kwargs):
        if url.endswith("/alarms"):
            return _response(503, {"status": "failed", "reason": "memory alarm on rabbit@a"})
        return _response(404, {"error": "not found"})

    session.request.side_effect = _respond
    checks = _client(session).health_checks()
    assert checks["alarms"]["status"] == "failed"
    assert "memory alarm" in checks["alarms"]["reason"]
    assert checks["local_alarms"]["status"] == "unsupported"


def test_as_page_slices_a_bare_list():
    page = rmq.as_page([{"name": "a"}, {"name": "b"}, {"name": "c"}], page=2, page_size=2)
    assert page["items"] == [{"name": "c"}]
    assert page["total_count"] == 3

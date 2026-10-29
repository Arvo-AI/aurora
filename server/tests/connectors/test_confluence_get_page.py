"""Confluence page fetch: URL ids, and v1 when v2 /pages 404s."""
import pytest
import requests

from connectors.confluence_connector.client import (
    ConfluenceClient,
    parse_confluence_page_id,
)


def test_parse_page_id_from_page_and_blog_urls():
    assert parse_confluence_page_id(
        "https://acme.atlassian.net/wiki/spaces/OPS/pages/99/Runbook"
    ) == "99"
    assert parse_confluence_page_id(
        "https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=42"
    ) == "42"
    assert parse_confluence_page_id(
        "https://acme.atlassian.net/wiki/spaces/OPS/blog/2026/01/02/77/Post"
    ) == "77"
    assert parse_confluence_page_id("12345") is None


def _oauth_client() -> ConfluenceClient:
    return ConfluenceClient(
        "https://acme.atlassian.net",
        "token",
        auth_type="oauth",
        cloud_id="cloud-1",
    )


def test_v2_hit_does_not_call_v1(monkeypatch):
    client = _oauth_client()
    monkeypatch.setattr(client, "_request_v2", lambda *a, **k: {"id": "99", "title": "Page"})
    called = {}

    def _v1(*_a, **_k):
        called["v1"] = True
        return {}

    monkeypatch.setattr(client, "_request", _v1)
    assert client.get_page("99")["id"] == "99"
    assert "v1" not in called


def test_v2_404_falls_back_to_v1_content(monkeypatch):
    client = _oauth_client()
    err = requests.HTTPError("missing")
    err.response = type("Resp", (), {"status_code": 404})()

    def _v2(*_a, **_k):
        raise err

    seen = {}

    def _v1(method, path, params=None):
        seen["method"] = method
        seen["path"] = path
        return {"id": "77", "title": "Post", "body": {"storage": {"value": "<p>x</p>"}}}

    monkeypatch.setattr(client, "_request_v2", _v2)
    monkeypatch.setattr(client, "_request", _v1)
    page = client.get_page(
        "https://acme.atlassian.net/wiki/spaces/OPS/blog/2026/01/02/77/Post"
    )
    assert page["id"] == "77"
    assert seen == {"method": "GET", "path": "/content/77"}


def test_v2_error_other_than_404_is_not_retried(monkeypatch):
    client = _oauth_client()
    err = requests.HTTPError("forbidden")
    err.response = type("Resp", (), {"status_code": 403})()

    def _v2(*_a, **_k):
        raise err

    def _v1(*_a, **_k):
        raise AssertionError("v1 should not run")

    monkeypatch.setattr(client, "_request_v2", _v2)
    monkeypatch.setattr(client, "_request", _v1)
    with pytest.raises(requests.HTTPError):
        client.get_page("99")

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
    # A numeric title is not the id, and a date with no id is not a page.
    assert parse_confluence_page_id(
        "https://acme.atlassian.net/wiki/spaces/OPS/blog/2026/01/02/77/123"
    ) == "77"
    assert parse_confluence_page_id(
        "https://acme.atlassian.net/wiki/spaces/OPS/blog/2026/01/02"
    ) is None
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


def test_v2_404_falls_back_to_v2_blogpost(monkeypatch):
    client = _oauth_client()
    err = requests.HTTPError("missing")
    err.response = type("Resp", (), {"status_code": 404})()
    seen = []

    def _v2(_method, path, params=None):
        seen.append(path)
        # Pages 404. Blog posts are the other v2 route, still inside the OAuth scopes.
        if path.startswith("/pages/"):
            raise err
        return {"id": "77", "title": "Post", "body": {"storage": {"value": "<p>x</p>"}}}

    def _v1(*_a, **_k):
        raise AssertionError("v1 content is outside the granular scopes")

    monkeypatch.setattr(client, "_request_v2", _v2)
    monkeypatch.setattr(client, "_request", _v1)
    page = client.get_page(
        "https://acme.atlassian.net/wiki/spaces/OPS/blog/2026/01/02/77/123"
    )
    assert page["id"] == "77"
    assert seen == ["/pages/77", "/blogposts/77"]


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

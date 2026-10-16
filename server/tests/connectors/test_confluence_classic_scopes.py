"""Cloud page reads follow the classic scopes in the Edit Scopes dialog."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from connectors.atlassian_auth.auth import CONFLUENCE_SCOPES, build_scopes
from connectors.confluence_connector.client import ConfluenceClient


def test_requested_scopes_match_the_classic_edit_scopes_dialog():
    assert CONFLUENCE_SCOPES.split() == [
        "read:confluence-content.all",
        "read:confluence-space.summary",
        "read:confluence-user",
        "search:confluence",
    ]
    granted = build_scopes(["confluence"])
    assert "read:page:confluence" not in granted
    assert "read:confluence-content.all" in granted


def _response(status, payload=None):
    response = MagicMock()
    response.status_code = status
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
        response.json.return_value = payload
    return response


def _cloud_client():
    return ConfluenceClient(
        "https://acme.atlassian.net",
        "token",
        auth_type="oauth",
        cloud_id="cloud-1",
    )


def test_page_read_uses_v1_for_classic_scopes():
    client = _cloud_client()
    body = {"id": "1", "body": {"storage": {"value": "<p>hi</p>"}}}
    with patch(
        "connectors.confluence_connector.client.requests.request",
        return_value=_response(200, body),
    ) as req:
        assert client.get_page("1") == body
    assert req.call_count == 1
    assert req.call_args.args[1].endswith("/rest/api/content/1")


def test_page_read_falls_back_to_v2_when_v1_returns_403():
    client = _cloud_client()
    body = {"id": "1", "body": {"storage": {"value": "<p>v2</p>"}}}
    with patch(
        "connectors.confluence_connector.client.requests.request",
        side_effect=[_response(403), _response(200, body)],
    ) as req:
        assert client.get_page("1") == body
    urls = [call.args[1] for call in req.call_args_list]
    assert urls[0].endswith("/rest/api/content/1")
    assert urls[1].endswith("/wiki/api/v2/pages/1")


def test_page_read_uses_v2_when_v1_returns_401_for_a_granular_token():
    client = _cloud_client()
    body = {"id": "1", "title": "Runbook"}
    with patch(
        "connectors.confluence_connector.client.requests.request",
        side_effect=[_response(401), _response(200, body)],
    ) as req:
        assert client.get_page("1") == body
    assert req.call_count == 2


def test_expired_classic_token_keeps_the_401_when_v2_also_rejects():
    client = _cloud_client()
    with patch(
        "connectors.confluence_connector.client.requests.request",
        side_effect=[_response(401), _response(403)],
    ):
        with pytest.raises(requests.HTTPError) as exc:
            client.get_page("1")
    assert exc.value.response.status_code == 401


def test_pat_page_read_stays_on_v1():
    client = ConfluenceClient("https://confluence.example.com", "pat", auth_type="pat")
    body = {"id": "9"}
    with patch(
        "connectors.confluence_connector.client.requests.request",
        return_value=_response(200, body),
    ) as req:
        assert client.get_page("9") == body
    assert req.call_count == 1
    assert "/wiki/api/v2/" not in req.call_args.args[1]

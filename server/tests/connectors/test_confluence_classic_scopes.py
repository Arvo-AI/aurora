"""Cloud page reads follow the classic scopes in the Edit Scopes dialog.

Loaded from the module files directly. The Confluence package init imports
BeautifulSoup, which the pre-check job does not install.
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SERVER = Path(__file__).resolve().parents[2]


def _ensure_requests():
    try:
        import requests
        return requests
    except ModuleNotFoundError:
        # Pre-checks install a short dependency list that omits requests.
        requests = types.ModuleType("requests")

        class RequestException(Exception):
            pass

        class HTTPError(RequestException):
            def __init__(self, *args, response=None, **kwargs):
                super().__init__(*args)
                self.response = response

        requests.RequestException = RequestException
        requests.HTTPError = HTTPError
        requests.request = MagicMock()
        requests.post = MagicMock()
        sys.modules["requests"] = requests
        return requests


def _load(module_name: str, relative: str):
    spec = importlib.util.spec_from_file_location(module_name, _SERVER / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


requests = _ensure_requests()
_auth = _load("atlassian_auth_under_test", "connectors/atlassian_auth/auth.py")
_client = _load("confluence_client_under_test", "connectors/confluence_connector/client.py")
CONFLUENCE_SCOPES = _auth.CONFLUENCE_SCOPES
build_scopes = _auth.build_scopes
ConfluenceClient = _client.ConfluenceClient


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
    with patch.object(_client.requests, "request", return_value=_response(200, body)) as req:
        assert client.get_page("1") == body
    assert req.call_count == 1
    assert req.call_args.args[1].endswith("/rest/api/content/1")


def test_page_read_falls_back_to_v2_when_v1_returns_403():
    client = _cloud_client()
    body = {"id": "1", "body": {"storage": {"value": "<p>v2</p>"}}}
    with patch.object(
        _client.requests,
        "request",
        side_effect=[_response(403), _response(200, body)],
    ) as req:
        assert client.get_page("1") == body
    urls = [call.args[1] for call in req.call_args_list]
    assert urls[0].endswith("/rest/api/content/1")
    assert urls[1].endswith("/wiki/api/v2/pages/1")


def test_page_read_uses_v2_when_v1_returns_401_for_a_granular_token():
    client = _cloud_client()
    body = {"id": "1", "title": "Runbook"}
    with patch.object(
        _client.requests,
        "request",
        side_effect=[_response(401), _response(200, body)],
    ) as req:
        assert client.get_page("1") == body
    assert req.call_count == 2


def test_expired_classic_token_keeps_the_401_when_v2_also_rejects():
    client = _cloud_client()
    with patch.object(
        _client.requests,
        "request",
        side_effect=[_response(401), _response(403)],
    ):
        with pytest.raises(requests.HTTPError) as exc:
            client.get_page("1")
    assert exc.value.response.status_code == 401


def test_pat_page_read_stays_on_v1():
    client = ConfluenceClient("https://confluence.example.com", "pat", auth_type="pat")
    body = {"id": "9"}
    with patch.object(_client.requests, "request", return_value=_response(200, body)) as req:
        assert client.get_page("9") == body
    assert req.call_count == 1
    assert "/wiki/api/v2/" not in req.call_args.args[1]

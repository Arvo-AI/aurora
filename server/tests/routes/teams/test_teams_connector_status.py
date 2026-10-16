"""Connector status for Teams must survive the ~1h access-token expiry."""

from unittest.mock import MagicMock

import pytest

from connectors.teams_connector import client as tc
from routes import connector_status as cs


def test_status_goes_through_the_refreshing_client(monkeypatch):
    client = MagicMock()
    asked = []
    monkeypatch.setattr(tc, "get_teams_client_for_user", lambda user_id: asked.append(user_id) or client)
    assert cs._check_teams({"access_token": "stale", "_user_id": "uid-owner"}) == {"connected": True}
    # Resolved for the token owner, using the client rather than the raw stored token
    assert asked == ["uid-owner"]
    client.get_me.assert_called_once()


def test_status_is_disconnected_when_no_usable_token(monkeypatch):
    monkeypatch.setattr(tc, "get_teams_client_for_user", lambda user_id: None)
    assert cs._check_teams({"access_token": "stale", "_user_id": "uid-owner"}) == {"connected": False}


def test_status_is_disconnected_when_graph_rejects_the_token(monkeypatch):
    client = MagicMock()
    client.get_me.side_effect = tc.TeamsAPIError("Graph API error 401", status_code=401)
    monkeypatch.setattr(tc, "get_teams_client_for_user", lambda user_id: client)
    assert cs._check_teams({"_user_id": "uid-owner"}) == {"connected": False}


def test_status_without_an_owner_is_disconnected(monkeypatch):
    monkeypatch.setattr(tc, "get_teams_client_for_user", lambda user_id: pytest.fail("no owner to resolve"))
    assert cs._check_teams({"access_token": "tok"}) == {"connected": False}


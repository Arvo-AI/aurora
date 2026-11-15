"""Which incident.io API-key roles can receive an RCA post-back."""

import json
from unittest.mock import MagicMock

from routes.incidentio.incidentio_client import (
    INCIDENTIO_TIMEOUT,
    IncidentioAPIError,
    IncidentioClient,
    _POSTBACK_IDENTITY_TIMEOUT,
    classify_postback_roles,
    postback_can_enable,
)


def test_edit_incidents_role_writes_incidents_only():
    assert classify_postback_roles(["viewer", "incident_editor"]) == {
        "incidents": True,
        "alerts": False,
        "alertsScoped": False,
    }


def test_on_call_role_writes_alerts_only():
    assert classify_postback_roles(["viewer", "on_call_editor"]) == {
        "incidents": False,
        "alerts": True,
        "alertsScoped": False,
    }


def test_team_scoped_on_call_role_is_not_full_alert_write():
    # team_roles cover only that key's teams, so another team's alert still 403s.
    assert classify_postback_roles(["viewer"], ["on_call_editor"]) == {
        "incidents": False,
        "alerts": False,
        "alertsScoped": True,
    }


def test_account_on_call_role_covers_every_team():
    # An account role already writes every alert; team_roles do not narrow it.
    assert classify_postback_roles(["on_call_editor"], ["on_call_editor"]) == {
        "incidents": False,
        "alerts": True,
        "alertsScoped": False,
    }


def test_neither_write_role():
    assert classify_postback_roles(["viewer", "global_access"]) == {
        "incidents": False,
        "alerts": False,
        "alertsScoped": False,
    }


def test_both_write_roles():
    assert classify_postback_roles(["incident_editor", "on_call_editor"]) == {
        "incidents": True,
        "alerts": True,
        "alertsScoped": False,
    }


def test_incident_editor_plus_team_on_call_stays_partial():
    # Full access would cache for 5 minutes and hide the other-team 403.
    assert classify_postback_roles(["incident_editor"], ["on_call_editor"]) == {
        "incidents": True,
        "alerts": False,
        "alertsScoped": True,
    }


def test_unreachable_identity_does_not_block_the_toggle():
    assert postback_can_enable({"checked": False, "incidents": False, "alerts": False}) is True


def test_key_that_writes_nowhere_cannot_enable():
    assert postback_can_enable({"checked": True, "incidents": False, "alerts": False}) is False


def test_partial_write_can_still_enable():
    assert postback_can_enable({"checked": True, "incidents": True, "alerts": False}) is True


def test_team_scoped_alert_write_can_still_enable():
    assert postback_can_enable(
        {"checked": True, "incidents": False, "alerts": False, "alertsScoped": True}
    ) is True


def test_read_postback_access_reads_identity_roles(monkeypatch):
    client = IncidentioClient("test-key")
    monkeypatch.setattr(
        client,
        "get_identity",
        lambda **_kwargs: {"identity": {"roles": ["incident_editor"], "team_roles": []}},
    )
    assert client.read_postback_access() == {
        "checked": True, "incidents": True, "alerts": False, "alertsScoped": False,
    }


def test_read_postback_access_uses_a_short_identity_timeout(monkeypatch):
    client = IncidentioClient("test-key")
    seen = {}

    def _request(method, path, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        response = MagicMock()
        response.json.return_value = {"identity": {"roles": ["incident_editor"], "team_roles": []}}
        return response

    monkeypatch.setattr(client, "_request", _request)
    client.read_postback_access()
    assert seen["timeout"] == _POSTBACK_IDENTITY_TIMEOUT
    assert seen["timeout"] < INCIDENTIO_TIMEOUT


def test_get_postback_access_refetches_when_cache_has_partial_access(monkeypatch):
    from routes.incidentio import tasks

    rc = MagicMock()
    rc.get.return_value = json.dumps({"checked": True, "incidents": True, "alerts": False})
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: rc)
    monkeypatch.setattr(
        "utils.auth.token_management.get_token_data",
        lambda *_args, **_kwargs: {"api_key": "k"},
    )

    client = MagicMock()
    client.read_postback_access.return_value = {"checked": True, "incidents": True, "alerts": True}
    monkeypatch.setattr("routes.incidentio.incidentio_client.IncidentioClient", lambda *_args, **_kwargs: client)

    assert tasks.get_postback_access("u1")["alerts"] is True
    client.read_postback_access.assert_called_once()
    rc.setex.assert_called_once()


def test_get_postback_access_does_not_cache_team_scoped_alert_write(monkeypatch):
    from routes.incidentio import tasks

    rc = MagicMock()
    rc.get.return_value = None
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: rc)
    monkeypatch.setattr(
        "utils.auth.token_management.get_token_data",
        lambda *_args, **_kwargs: {"api_key": "k"},
    )

    client = MagicMock()
    client.read_postback_access.return_value = {
        "checked": True, "incidents": True, "alerts": False, "alertsScoped": True,
    }
    monkeypatch.setattr("routes.incidentio.incidentio_client.IncidentioClient", lambda *_args, **_kwargs: client)

    assert tasks.get_postback_access("u1")["alertsScoped"] is True
    rc.setex.assert_not_called()


def test_get_postback_access_does_not_cache_partial_access(monkeypatch):
    from routes.incidentio import tasks

    rc = MagicMock()
    rc.get.return_value = None
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: rc)
    monkeypatch.setattr(
        "utils.auth.token_management.get_token_data",
        lambda *_args, **_kwargs: {"api_key": "k"},
    )

    client = MagicMock()
    client.read_postback_access.return_value = {"checked": True, "incidents": True, "alerts": False}
    monkeypatch.setattr("routes.incidentio.incidentio_client.IncidentioClient", lambda *_args, **_kwargs: client)

    tasks.get_postback_access("u1")
    rc.setex.assert_not_called()


def test_get_postback_access_uses_redis_cache(monkeypatch):
    from routes.incidentio import tasks

    cached = {"checked": True, "incidents": True, "alerts": True}
    rc = MagicMock()
    rc.get.return_value = json.dumps(cached)
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: rc)

    def _boom(*_args, **_kwargs):
        raise AssertionError("identity should not be called on cache hit")

    monkeypatch.setattr("utils.auth.token_management.get_token_data", _boom)
    assert tasks.get_postback_access("u1") == cached


def test_read_postback_access_is_unchecked_when_identity_fails(monkeypatch):
    client = IncidentioClient("test-key")

    def _boom(**_kwargs):
        raise IncidentioAPIError(IncidentioAPIError.TIMEOUT)

    monkeypatch.setattr(client, "get_identity", _boom)
    assert client.read_postback_access() == {
        "checked": False, "incidents": False, "alerts": False, "alertsScoped": False,
    }

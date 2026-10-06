"""Which incident.io API-key roles can receive an RCA post-back."""

import json
from unittest.mock import MagicMock

from routes.incidentio.incidentio_client import (
    IncidentioAPIError,
    IncidentioClient,
    classify_postback_roles,
    postback_can_enable,
)


def test_edit_incidents_role_writes_incidents_only():
    assert classify_postback_roles(["viewer", "incident_editor"]) == {
        "incidents": True,
        "alerts": False,
    }


def test_on_call_role_writes_alerts_only():
    assert classify_postback_roles(["viewer", "on_call_editor"]) == {
        "incidents": False,
        "alerts": True,
    }


def test_team_scoped_on_call_role_still_writes_alerts():
    # Create and manage on call ressources can be granted for specific teams only.
    assert classify_postback_roles(["viewer"], ["on_call_editor"]) == {
        "incidents": False,
        "alerts": True,
    }


def test_neither_write_role():
    assert classify_postback_roles(["viewer", "global_access"]) == {
        "incidents": False,
        "alerts": False,
    }


def test_both_write_roles():
    assert classify_postback_roles(["incident_editor", "on_call_editor"]) == {
        "incidents": True,
        "alerts": True,
    }


def test_unreachable_identity_does_not_block_the_toggle():
    assert postback_can_enable({"checked": False, "incidents": False, "alerts": False}) is True


def test_key_that_writes_nowhere_cannot_enable():
    assert postback_can_enable({"checked": True, "incidents": False, "alerts": False}) is False


def test_partial_write_can_still_enable():
    assert postback_can_enable({"checked": True, "incidents": True, "alerts": False}) is True


def test_read_postback_access_reads_identity_roles(monkeypatch):
    client = IncidentioClient("test-key")
    monkeypatch.setattr(
        client,
        "get_identity",
        lambda: {"identity": {"roles": ["incident_editor"], "team_roles": []}},
    )
    assert client.read_postback_access() == {"checked": True, "incidents": True, "alerts": False}


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

    def _boom():
        raise IncidentioAPIError(IncidentioAPIError.TIMEOUT)

    monkeypatch.setattr(client, "get_identity", _boom)
    assert client.read_postback_access() == {"checked": False, "incidents": False, "alerts": False}

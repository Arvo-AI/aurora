"""Shared incidents-channel prefs: clearing must not resurrect a disconnected provider."""

import pytest

from services.channels import prefs


@pytest.fixture
def env(monkeypatch):
    state = {"creds": {}, "stored": [], "org_prefs": {}}
    monkeypatch.setattr(prefs, "get_org_id_for_user", lambda user_id: "org-1")
    monkeypatch.setattr(prefs, "get_credentials_from_db", lambda user_id, provider: dict(state["creds"]))
    monkeypatch.setattr(prefs, "store_org_preference", lambda org, key, value: state["org_prefs"].__setitem__(key, value))
    monkeypatch.setattr(prefs, "store_tokens_in_db", lambda uid, data, prov: state["stored"].append((uid, data, prov)))
    return state


@pytest.mark.parametrize("provider", ["slack", "teams"])
def test_clearing_for_a_never_connected_provider_writes_no_secret(env, provider):
    env["creds"] = {}
    prefs.clear_incidents_channel("uid-self", provider)
    assert env["stored"] == []
    # The org preference is still cleared
    assert env["org_prefs"][f"{provider}_incidents_channel_id"] == ""


@pytest.mark.parametrize("provider", ["slack", "teams"])
def test_clearing_a_connected_provider_keeps_the_rest_of_its_creds(env, provider):
    env["creds"] = {"access_token": "tok", "tenant_id": "tenant-1",
                    "incidents_channel_id": "c1", "incidents_channel_name": "alerts"}
    prefs.clear_incidents_channel("uid-self", provider)
    (uid, data, prov), = env["stored"]
    assert (uid, prov) == ("uid-self", provider)
    assert data == {"access_token": "tok", "tenant_id": "tenant-1"}

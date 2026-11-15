"""Entra OAuth config: a blank tenant must be reported as a setup problem."""

import pytest

from connectors.teams_connector import oauth


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TEAMS_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "https://aurora.example.com")
    monkeypatch.delenv("NGROK_URL", raising=False)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_tenant_asks_for_a_developer(configured, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TEAMS_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("TEAMS_TENANT_ID", value)
    with pytest.raises(ValueError) as exc:
        oauth.get_auth_url(state="s")
    message = str(exc.value)
    assert "TEAMS_TENANT_ID" in message
    assert "developer" in message


def test_blank_tenant_never_builds_a_double_slash_url(configured, monkeypatch):
    monkeypatch.setenv("TEAMS_TENANT_ID", "")
    with pytest.raises(ValueError):
        oauth.get_auth_url(state="s")


def test_tenant_guid_is_used_in_the_authorize_url(configured, monkeypatch):
    monkeypatch.setenv("TEAMS_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    url = oauth.get_auth_url(state="s")
    assert url.startswith("https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/oauth2/v2.0/authorize?")
    assert "state=s" in url
    assert "prompt=select_account" in url


def test_explicit_common_is_still_allowed(configured, monkeypatch):
    monkeypatch.setenv("TEAMS_TENANT_ID", "common")
    assert "/common/oauth2/v2.0/authorize" in oauth.get_auth_url(state="s")


def test_missing_client_id_is_listed_with_the_tenant(configured, monkeypatch):
    monkeypatch.delenv("TEAMS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TEAMS_TENANT_ID", raising=False)
    with pytest.raises(ValueError) as exc:
        oauth.get_auth_url(state="s")
    assert "TEAMS_CLIENT_ID" in str(exc.value) and "TEAMS_TENANT_ID" in str(exc.value)


def test_state_is_required(configured, monkeypatch):
    monkeypatch.setenv("TEAMS_TENANT_ID", "common")
    with pytest.raises(ValueError):
        oauth.get_auth_url(state="")

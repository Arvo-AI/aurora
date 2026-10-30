"""Ask-mode Azure refresh must not overwrite the agent service principal."""

import time
from types import SimpleNamespace


_AGENT_CLIENT_ID = "agent-app"
_AGENT_CLIENT_SECRET = "agent-secret"
_READ_ONLY_CLIENT_ID = "readonly-app"
_READ_ONLY_CLIENT_SECRET = "readonly-secret"
_NEW_ACCESS_TOKEN = "minted-access-token"


def _stored_token(**overrides):
    token = {
        "tenant_id": "tenant-1",
        "client_id": _AGENT_CLIENT_ID,
        "client_secret": _AGENT_CLIENT_SECRET,
        "subscription_id": "sub-default",
        "access_token": "stale-access-token",
        "read_only": {
            "tenant_id": "tenant-1",
            "client_id": _READ_ONLY_CLIENT_ID,
            "client_secret": _READ_ONLY_CLIENT_SECRET,
            "subscription_id": "sub-default",
        },
    }
    token.update(overrides)
    return token


def _patch_azure_refresh(monkeypatch, stored, minted_client_ids):
    """Stub Vault, the token owner lookup, and the Azure credential."""

    class _Credential:
        def __init__(self, tenant_id, client_id, client_secret):
            minted_client_ids.append((tenant_id, client_id, client_secret))

        def get_token(self, scope):
            assert scope == "https://management.azure.com/.default"
            return SimpleNamespace(token=_NEW_ACCESS_TOKEN, expires_on=1_900_000_000)

    monkeypatch.setattr("azure.identity.ClientSecretCredential", _Credential)
    monkeypatch.setattr(
        "utils.auth.token_management.get_token_data",
        lambda user_id, provider: stored,
    )
    monkeypatch.setattr(
        "utils.secrets.secret_ref_utils.get_token_owner_id",
        lambda user_id, provider: user_id,
    )

    saved = {}

    def _store(owner_id, token_data, provider, **kwargs):
        assert provider == "azure"
        saved["owner_id"] = owner_id
        saved["data"] = token_data

    monkeypatch.setattr("utils.auth.token_management.store_tokens_in_db", _store)
    return saved


def test_ask_mode_refresh_keeps_the_agent_service_principal(monkeypatch):
    """A read-only mint must not be written back over the stored agent app."""
    from utils.auth.cloud_auth import generate_azure_access_token

    stored = _stored_token()
    minted_client_ids = []
    saved = _patch_azure_refresh(monkeypatch, stored, minted_client_ids)

    result = generate_azure_access_token("uid-1", subscription_id="sub-targeted", mode="ask")

    # Ask mode still mints with the read-only principal.
    assert minted_client_ids == [("tenant-1", _READ_ONLY_CLIENT_ID, _READ_ONLY_CLIENT_SECRET)]
    assert result["client_id"] == _READ_ONLY_CLIENT_ID
    assert result["access_token"] == _NEW_ACCESS_TOKEN
    # The caller's subscription is used for this call only.
    assert result["subscription_id"] == "sub-targeted"

    persisted = saved["data"]
    assert persisted["client_id"] == _AGENT_CLIENT_ID
    assert persisted["client_secret"] == _AGENT_CLIENT_SECRET
    assert persisted["read_only"]["client_id"] == _READ_ONLY_CLIENT_ID
    assert persisted["read_only"]["client_secret"] == _READ_ONLY_CLIENT_SECRET
    assert persisted["access_token"] == _NEW_ACCESS_TOKEN
    assert persisted["subscription_id"] == "sub-default"


def test_ask_mode_reuse_returns_read_only_principal_without_storing(monkeypatch):
    """A fresh ask-mode token is reused with the read-only app, not the agent app."""
    from utils.auth.cloud_auth import generate_azure_access_token

    stored = _stored_token(
        mode="ask",
        expires_on=time.time() + 3600,
        access_token="cached-access-token",
    )
    minted_client_ids = []
    saved = _patch_azure_refresh(monkeypatch, stored, minted_client_ids)

    result = generate_azure_access_token("uid-1", mode="ask")

    assert minted_client_ids == []
    assert saved == {}
    assert result["client_id"] == _READ_ONLY_CLIENT_ID
    assert result["client_secret"] == _READ_ONLY_CLIENT_SECRET
    assert result["access_token"] == "cached-access-token"

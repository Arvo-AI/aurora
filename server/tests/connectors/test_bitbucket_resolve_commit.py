"""Bitbucket /src/ cannot take slashy branch names — _resolve_commit must hash them."""
from unittest.mock import MagicMock

from connectors.bitbucket_connector.api_client import BitbucketAPIClient


def _client_with_gets(responses):
    client = BitbucketAPIClient("token")
    client._get = MagicMock(side_effect=responses)
    return client


def test_head_with_slashy_default_branch_resolves_to_hash():
    client = _client_with_gets([
        {"mainbranch": {"name": "fix/aurora-abc", "type": "branch"}},
        {"target": {"hash": "deadbeefcafef00d1234567890abcdef12345678"}},
    ])
    assert client._resolve_commit("ws", "repo", "HEAD") == "deadbeefcafef00d1234567890abcdef12345678"
    # Second call must hit /refs/branches/ with the slash encoded, not /src/
    assert "refs/branches/fix%2Faurora-abc" in client._get.call_args_list[1].args[0]


def test_slashy_branch_resolves_to_hash():
    client = _client_with_gets([
        {"target": {"hash": "abc1234567890"}},
    ])
    assert client._resolve_commit("ws", "repo", "feature/x") == "abc1234567890"


def test_tag_used_when_branch_missing():
    client = _client_with_gets([
        {"error": True, "status": 404},
        {"target": {"hash": "taghash000001"}},
    ])
    assert client._resolve_commit("ws", "repo", "v1.2.3") == "taghash000001"


def test_sha_passthrough():
    client = _client_with_gets([])
    sha = "2e94a766f8f2f12b4664d6befd4737a60143808f"
    assert client._resolve_commit("ws", "repo", sha) == sha
    client._get.assert_not_called()


def test_alphanumeric_branch_is_not_treated_as_sha():
    """Regression: old isalnum() check would skip resolving release20240101."""
    client = _client_with_gets([
        {"target": {"hash": "realhash000001"}},
    ])
    assert client._resolve_commit("ws", "repo", "release20240101") == "realhash000001"

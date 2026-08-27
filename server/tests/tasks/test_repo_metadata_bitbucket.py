"""Bitbucket metadata: empty repos are not failures; fetch errors still are."""
from unittest.mock import MagicMock, patch

from utils.repo_metadata import _fetch_bitbucket_context


def _client(listing, repo=None):
    client = MagicMock()
    client.get_repository.return_value = repo or {"mainbranch": {"name": "main"}}
    client._resolve_commit.return_value = "abc1234"
    client.get_directory_tree.return_value = listing
    return client


@patch("connectors.bitbucket_connector.api_client.BitbucketAPIClient")
def test_empty_tree_is_not_a_fetch_failure(mock_cls):
    mock_cls.return_value = _client({"error": True, "status": 404})
    assert _fetch_bitbucket_context("tok", "api_token", "ws", "empty") == ("", "")
    mock_cls.return_value.get_file_contents.assert_not_called()


@patch("connectors.bitbucket_connector.api_client.BitbucketAPIClient")
def test_size_zero_skips_src_hunt(mock_cls):
    mock_cls.return_value = _client(
        {"error": True, "status": 404},
        repo={"size": 0, "mainbranch": {"name": "master"}},
    )
    assert _fetch_bitbucket_context("tok", "api_token", "ws", "empty") == ("", "")
    mock_cls.return_value.get_directory_tree.assert_not_called()
    mock_cls.return_value.get_file_contents.assert_not_called()


@patch("connectors.bitbucket_connector.api_client.BitbucketAPIClient")
def test_forbidden_listing_is_a_fetch_failure(mock_cls):
    mock_cls.return_value = _client({"error": True, "status": 403})
    assert _fetch_bitbucket_context("tok", "api_token", "ws", "priv") == (
        "",
        "(could not list files)",
    )

"""Bitbucket metadata: empty repos are not failures; fetch errors still are."""
from unittest.mock import MagicMock, patch

from utils.repo_metadata import (
    _EMPTY_SUMMARY,
    _fetch_bitbucket_context,
    generate_repo_metadata,
)


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


@patch("utils.repo_metadata._generate_summary")
@patch("utils.repo_metadata._update_metadata")
@patch("utils.repo_metadata._fetch_repo_context", return_value=("", ""))
@patch("utils.repo_metadata._get_credentials", return_value={"access_token": "x"})
def test_empty_repo_marked_ready(get_creds, fetch, update, summarize):
    generate_repo_metadata.run("user-1", "bitbucket", "ws/empty")
    summarize.assert_not_called()
    update.assert_called_with("user-1", "bitbucket", "ws/empty", _EMPTY_SUMMARY, "ready")


@patch("utils.repo_metadata._generate_summary")
@patch("utils.repo_metadata._update_metadata")
@patch("utils.repo_metadata._fetch_repo_context", return_value=("", "(could not list files)"))
@patch("utils.repo_metadata._get_credentials", return_value={"access_token": "x"})
def test_fetch_failure_marked_error(get_creds, fetch, update, summarize):
    generate_repo_metadata.run("user-1", "bitbucket", "ws/oops")
    summarize.assert_not_called()
    update.assert_called_with("user-1", "bitbucket", "ws/oops", None, "error")

"""Tests for services.change_gating.backtest."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.change_gating.backtest import (
    BacktestError,
    build_backtest_prompt,
    extract_reverted_ref,
    find_candidates,
    is_revert_title,
    parse_repo_url,
    review_pr,
)

REPO = "acme/api"

MERGED_PR = {
    "number": 42,
    "title": "Lower DB pool size",
    "body": "Edited after merge: this caused the outage, reverted in #50",
    "html_url": "https://github.com/acme/api/pull/42",
    "merged_at": "2026-10-01T00:00:00Z",
    "user": {"login": "dev"},
    "base": {"ref": "main", "sha": "b" * 40, "repo": {"private": False}},
    "head": {"ref": "pool", "sha": "a" * 40},
}
FILES = [{
    "filename": "deploy/values.yaml",
    "status": "modified",
    "additions": 1,
    "deletions": 1,
    "patch": "@@ -1,1 +1,1 @@\n-pool_size: 50\n+pool_size: 2",
}]


class TestParseRepoUrl:
    @pytest.mark.parametrize("value", [
        "https://github.com/acme/api",
        "https://github.com/acme/api/",
        "https://github.com/acme/api.git",
        "github.com/acme/api",
        "acme/api",
    ])
    def test_accepts(self, value):
        assert parse_repo_url(value) == ("acme", "api")

    @pytest.mark.parametrize("value", [
        "",
        "https://evil.com/acme/api",
        "https://github.com.evil.com/acme/api",
        "https://github.com/acme",
        "https://github.com/acme/api/../../x",
        "acme/..",
        "http://169.254.169.254/latest",
    ])
    def test_rejects(self, value):
        with pytest.raises(BacktestError):
            parse_repo_url(value)


class TestExtractRevertedRef:
    def test_github_revert_button_body(self):
        assert extract_reverted_ref('Revert "Lower pool"', "Reverts acme/api#42", REPO) == (42, None)

    def test_ignores_other_repo_reference(self):
        assert extract_reverted_ref("Revert x", "Reverts other/repo#42", REPO) == (None, None)

    def test_squash_title(self):
        assert extract_reverted_ref('Revert "Lower pool (#42)" (#50)', "", REPO) == (42, None)

    def test_git_revert_sha(self):
        sha = "abc1234def"
        assert extract_reverted_ref("Revert pool", f"This reverts commit {sha}.", REPO) == (None, sha)

    def test_revert_of_revert_is_skipped(self):
        assert is_revert_title('Revert "Lower pool"')
        assert not is_revert_title('Revert "Revert "Lower pool""')
        assert not is_revert_title("Add revert button")


class TestFindCandidates:
    def test_reverted_prs_never_become_controls(self):
        client = MagicMock()
        client.get_repo.return_value = {"private": False}
        client.search_prs.side_effect = [
            [
                {"number": 50, "title": 'Revert "Lower pool"', "body": "Reverts acme/api#42", "closed_at": "x"},
                {"number": 51, "title": 'Revert "Revert "Lower pool""', "body": "Reverts acme/api#50"},
                {"number": 52, "title": "Revert cache", "body": "This reverts commit abcdef1."},
            ],
            [
                {"number": 42, "title": "Lower pool"},
                {"number": 40, "title": "Revert-safe docs"},
                {"number": 30, "title": "Add metrics"},
                {"number": 31, "title": "Bump deps"},
                {"number": 32, "title": "Extra"},
            ],
        ]
        client.pulls_for_commit.return_value = [{"number": 45, "merged_at": "y"}]

        result = find_candidates(client, "acme", "api", now=datetime(2026, 11, 9, tzinfo=timezone.utc))

        assert [c["pr_number"] for c in result["reverted"]] == [42, 45]
        assert [c["pr_number"] for c in result["controls"]] == [30, 31]
        control_query = client.search_prs.call_args_list[1].args[0]
        assert "merged:<2026-10-10" in control_query
        assert client.search_prs.call_count == 2

    def test_private_repo_refused_before_search(self):
        client = MagicMock()
        client.get_repo.return_value = {"private": True}
        with pytest.raises(BacktestError) as exc:
            find_candidates(client, "acme", "api")
        assert exc.value.status_code == 403
        client.search_prs.assert_not_called()


class TestHindsightIsolation:
    def test_prompt_excludes_post_merge_body(self):
        prompt = build_backtest_prompt(REPO, MERGED_PR, FILES)
        assert "caused the outage" not in prompt
        assert "#50" not in prompt
        assert "pool_size: 2" in prompt
        assert "NO tools" in prompt

    def test_review_uses_only_pr_and_files(self):
        client = MagicMock()
        client.get_pull.return_value = MERGED_PR
        client.list_files.return_value = FILES
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=(
            '{"verdict": "RISKY", "summary": "Pool shrinks 25x.", "findings": [{"severity": "HIGH", '
            '"file_path": "deploy/values.yaml", "line": 1, "title": "Pool too small", '
            '"explanation": "Requests queue and time out."}]}'
        ))

        result = review_pr(client, REPO, 42, llm=llm)

        assert result["verdict"] == "RISKY"
        assert result["findings"][0]["title"] == "Pool too small"
        # Only merge-time endpoints: no comments, reviews, timeline or HEAD reads.
        called = {c[0] for c in client.method_calls}
        assert called == {"get_pull", "list_files"}

    def test_private_repo_refused(self):
        client = MagicMock()
        client.get_pull.return_value = {**MERGED_PR, "base": {"repo": {"private": True}}}
        with pytest.raises(BacktestError) as exc:
            review_pr(client, REPO, 42, llm=MagicMock())
        assert exc.value.status_code == 403
        client.list_files.assert_not_called()

    def test_unmerged_pr_refused(self):
        client = MagicMock()
        client.get_pull.return_value = {**MERGED_PR, "merged_at": None}
        with pytest.raises(BacktestError):
            review_pr(client, REPO, 42, llm=MagicMock())

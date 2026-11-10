"""Tests for the retro review: revert parsing, control picking, scoring,
leakage guard, the public GitHub client, and the per-PR review step."""

from unittest.mock import MagicMock, patch

import pytest

from services.change_gating import retro
from services.change_gating.public_github_client import (
    GitHubRateLimited,
    GitHubRepoNotFound,
    PublicGitHubClient,
)

REPO = "acme/widgets"


# ----------------------------------------------------------------------
# normalize_repo
# ----------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "acme/widgets",
    " acme/widgets ",
    "https://github.com/acme/widgets",
    "github.com/acme/widgets/",
    "https://github.com/acme/widgets.git",
    "https://github.com/acme/widgets/pulls?q=is%3Aopen",
])
def test_normalize_repo_accepts_names_and_urls(raw):
    assert retro.normalize_repo(raw) == REPO


@pytest.mark.parametrize("raw", [
    "", "acme", "../etc/passwd", "acme/widgets; rm -rf /",
    "https://gitlab.com/acme/widgets", "acme/wid gets",
])
def test_normalize_repo_rejects_everything_else(raw):
    with pytest.raises(ValueError):
        retro.normalize_repo(raw)


# ----------------------------------------------------------------------
# parse_reverted_pr_number
# ----------------------------------------------------------------------

def _pr(number, title, body=""):
    return {"number": number, "title": title, "body": body, "html_url": f"https://github.com/{REPO}/pull/{number}"}


def test_parses_github_revert_button_body():
    revert = _pr(120, 'Revert "Bump pool size"', f"Reverts {REPO}#101")
    assert retro.parse_reverted_pr_number(REPO, revert) == 101


def test_parses_short_body_form():
    assert retro.parse_reverted_pr_number(REPO, _pr(120, 'Revert "x"', "Reverts #101")) == 101


def test_parses_squash_merge_title_suffix():
    revert = _pr(120, 'Revert "Bump pool size (#101)"', "")
    assert retro.parse_reverted_pr_number(REPO, revert) == 101


def test_ignores_revert_of_revert():
    revert = _pr(130, 'Revert "Revert "Bump pool size""', f"Reverts {REPO}#120")
    assert retro.parse_reverted_pr_number(REPO, revert) is None


def test_ignores_titles_that_only_mention_revert():
    assert retro.parse_reverted_pr_number(REPO, _pr(5, "Fix revert handling in CLI", "Reverts #4")) is None


def test_ignores_vague_reverts_without_a_pr_number():
    assert retro.parse_reverted_pr_number(REPO, _pr(5, 'Revert "Bump pool size"', "This reverts commit abc123.")) is None


def test_ignores_body_pointing_at_another_repo():
    revert = _pr(120, 'Revert "Bump pool size"', "Reverts other/repo#101")
    assert retro.parse_reverted_pr_number(REPO, revert) is None


def test_repo_name_dots_are_literal():
    # Without re.escape, the "." in "vercel.next/js" would match any character.
    revert = _pr(120, 'Revert "x"', "Reverts vercelxnext/js#101")
    assert retro.parse_reverted_pr_number("vercel.next/js", revert) is None


def test_ignores_self_reference():
    assert retro.parse_reverted_pr_number(REPO, _pr(101, 'Revert "x (#101)"')) is None


# ----------------------------------------------------------------------
# find_reverted / pick_controls
# ----------------------------------------------------------------------

def test_find_reverted_dedupes_and_limits():
    reverts = [
        _pr(120, 'Revert "A (#101)"'),
        _pr(121, 'Revert "A again (#101)"'),  # same original
        _pr(122, 'Revert "B (#102)"'),
        _pr(123, 'Revert "C (#103)"'),
    ]
    found = retro.find_reverted(REPO, reverts, limit=2)
    assert [f["pr_number"] for f in found] == [101, 102]
    assert found[0] == {
        "pr_number": 101, "kind": "reverted", "revert_pr_number": 120,
        "revert_url": f"https://github.com/{REPO}/pull/120",
    }


def test_pick_controls_skips_reverted_and_revert_prs():
    merged = [_pr(101, "A"), _pr(120, 'Revert "A"'), _pr(99, "D"), _pr(98, "E")]
    controls = retro.pick_controls(merged, exclude={101, 120}, limit=5)
    assert [c["pr_number"] for c in controls] == [99, 98]
    assert all(c["kind"] == "control" for c in controls)


# ----------------------------------------------------------------------
# infra_files: only infra/deploy changes are in scope
# ----------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "Dockerfile", "services/api/Dockerfile.prod", "docker-compose.yaml",
    "infra/main.tf", "envs/prod.tfvars", "charts/api/values.yaml", "Chart.yaml",
    "k8s/deployment.yaml", ".github/workflows/ci.yml", "db/migrations/0042_add_index.py",
    "schema.sql", "config/settings.py", ".env.example", "pyproject.toml", "Procfile",
])
def test_infra_paths_are_in_scope(path):
    assert retro.infra_files([{"filename": path}]) == [path]


@pytest.mark.parametrize("path", [
    "src/app.py", "README.md", "docs/deploying.md", "web/components/Button.tsx",
    "tests/test_api.py", ".github/ISSUE_TEMPLATE/bug.md",
])
def test_app_code_is_out_of_scope(path):
    assert retro.infra_files([{"filename": path}]) == []


def test_rename_counts_by_old_name():
    files = [{"filename": "old/stuff.yaml", "previous_filename": "helm/stuff.yaml"}]
    assert retro.infra_files(files) == ["helm/stuff.yaml"]


# ----------------------------------------------------------------------
# Prompt: same as the live review, plus retro mode, minus hindsight
# ----------------------------------------------------------------------

def test_retro_prompt_reuses_live_review_and_disables_tools():
    pr = _pr(101, "Bump pool size", "Raises max connections")
    pr.update({"base": {"ref": "main"}, "head": {"ref": "pool", "sha": "abc"}, "user": {"login": "dev"}})
    files = [{"filename": "deploy/values.yaml", "status": "modified", "additions": 1,
              "deletions": 1, "patch": "@@ -1 +1 @@\n-pool: 10\n+pool: 500"}]
    prompt = retro.build_retro_prompt(REPO, pr, files, None)
    assert "OUTPUT FORMAT" in prompt  # live review instructions are intact
    assert "deploy/values.yaml" in prompt
    assert "RETROSPECTIVE MODE" in prompt
    assert "NO tools" in prompt
    # Nothing from after the merge is passed in, so nothing can leak.
    assert "revert" not in prompt.lower().replace("retrospective", "")


# ----------------------------------------------------------------------
# normalize_verdict / score
# ----------------------------------------------------------------------

def test_normalize_verdict_matches_live_review_rules():
    assert retro.normalize_verdict({"verdict": "SAFE", "findings": [{"x": 1}]})["findings"] == []
    demoted = retro.normalize_verdict({"verdict": "RISKY", "findings": []})
    assert demoted["verdict"] == "SAFE"


def test_score_counts_catches_and_false_alarms():
    items = [
        {"kind": "reverted", "status": "reviewed", "verdict": "RISKY"},
        {"kind": "reverted", "status": "reviewed", "verdict": "SAFE"},
        {"kind": "reverted", "status": "skipped"},
        {"kind": "reverted", "status": "out_of_scope"},
        {"kind": "control", "status": "reviewed", "verdict": "SAFE"},
        {"kind": "control", "status": "reviewed", "verdict": "SAFE"},
        {"kind": "control", "status": "reviewed", "verdict": "RISKY"},
        {"kind": "control", "status": "error"},
    ]
    s = retro.score(items)
    assert s == {
        "reverted_reviewed": 2, "caught": 1, "catch_rate": 0.5,
        "controls_reviewed": 3, "false_alarms": 1, "false_alarm_rate": 0.333,
        "skipped": 1, "errored": 1, "out_of_scope": 1,
    }


def test_score_with_nothing_reviewed_has_no_rates():
    s = retro.score([{"kind": "reverted", "status": "pending"}])
    assert s["catch_rate"] is None and s["false_alarm_rate"] is None


# ----------------------------------------------------------------------
# PublicGitHubClient
# ----------------------------------------------------------------------

def _response(status=200, json_data=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.headers = headers or {}
    return resp


@patch("services.change_gating.public_github_client.requests")
class TestPublicGitHubClient:

    def test_no_token_sends_no_authorization(self, mock_requests):
        http = mock_requests.Session.return_value
        http.get.return_value = _response(json_data={"full_name": REPO})
        PublicGitHubClient(REPO, token="").get_repo()
        headers = http.get.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_server_token_only_raises_rate_limit(self, mock_requests):
        http = mock_requests.Session.return_value
        http.get.return_value = _response(json_data={})
        PublicGitHubClient(REPO, token="ghp_" + "x").get_repo()
        assert http.get.call_args.kwargs["headers"]["Authorization"] == "Bearer ghp_x"

    def test_private_or_missing_repo(self, mock_requests):
        mock_requests.Session.return_value.get.return_value = _response(status=404)
        with pytest.raises(GitHubRepoNotFound):
            PublicGitHubClient(REPO, token="").get_repo()

    def test_rate_limit_is_a_distinct_error(self, mock_requests):
        mock_requests.Session.return_value.get.return_value = _response(
            status=403, headers={"X-RateLimit-Remaining": "0"})
        with pytest.raises(GitHubRateLimited):
            PublicGitHubClient(REPO, token="").get_repo()

    def test_revert_search_is_scoped_to_repo_and_merged_prs(self, mock_requests):
        http = mock_requests.Session.return_value
        http.get.return_value = _response(json_data={"items": [{"number": 1}]})
        items = PublicGitHubClient(REPO, token="").search_merged_revert_prs()
        query = http.get.call_args.kwargs["params"]["q"]
        assert f"repo:{REPO}" in query and "is:merged" in query and "is:pr" in query
        assert items == [{"number": 1}]

    def test_recent_merged_filters_unmerged(self, mock_requests):
        http = mock_requests.Session.return_value
        http.get.return_value = _response(json_data=[
            {"number": 1, "merged_at": "2026-01-01T00:00:00Z"},
            {"number": 2, "merged_at": None},
        ])
        assert [p["number"] for p in PublicGitHubClient(REPO, token="").list_recent_merged_prs()] == [1]


# ----------------------------------------------------------------------
# review_pr (the task's per-PR step), with GitHub and the model mocked
# ----------------------------------------------------------------------

@pytest.fixture
def retro_review(monkeypatch):
    """Import tasks.retro_review without a broker.

    Importing the real celery_config connects to Redis, so stub it with an
    app whose @task decorator returns the function unchanged.
    """
    import importlib
    import sys
    from types import ModuleType

    stub = ModuleType("celery_config")
    stub.celery_app = MagicMock()
    stub.celery_app.task = lambda *a, **k: (lambda fn: fn)
    monkeypatch.setitem(sys.modules, "celery_config", stub)
    monkeypatch.delitem(sys.modules, "tasks.retro_review", raising=False)
    # The verdict parser falls back to a real LLM call when the text has no
    # JSON verdict. Tests must never reach a model.
    monkeypatch.setattr(
        "services.change_gating.verdict.extract_verdict_with_llm", lambda text: None
    )
    return importlib.import_module("tasks.retro_review")


def _client_with(pr, files):
    client = MagicMock()
    client.get_pull_request.return_value = pr
    client.list_files.return_value = files
    client.get_diff.return_value = None
    return client


_MERGED_PR = {
    "number": 101, "title": "Bump pool size", "body": "", "html_url": "u",
    "merged_at": "2026-01-01T00:00:00Z", "user": {"login": "dev"},
    "base": {"ref": "main"}, "head": {"ref": "pool", "sha": "abc"},
}
_FILES = [{"filename": "values.yaml", "status": "modified", "additions": 1, "deletions": 1,
           "patch": "@@ -1 +1 @@\n-pool: 10\n+pool: 500"}]
_RISKY_JSON = (
    '{"verdict": "RISKY", "summary": "Pool exceeds DB max_connections.", "findings": ['
    '{"severity": "HIGH", "file_path": "values.yaml", "line": 1, "title": "Pool too large",'
    ' "explanation": "500 x replicas exceeds the database limit."}]}'
)


def test_review_pr_records_verdict(retro_review):
    with patch.object(retro_review, "_call_review_model", return_value=_RISKY_JSON) as call:
        item = retro_review.review_pr(_client_with(_MERGED_PR, _FILES), REPO,
                                      {"pr_number": 101, "kind": "reverted"})
    assert item["status"] == "reviewed"
    assert item["verdict"] == "RISKY"
    assert item["findings"][0]["title"] == "Pool too large"
    assert "RETROSPECTIVE MODE" in call.call_args.args[0]


def test_review_pr_skips_unmerged_prs_without_calling_the_model(retro_review):
    with patch.object(retro_review, "_call_review_model") as call:
        item = retro_review.review_pr(_client_with({**_MERGED_PR, "merged_at": None}, _FILES),
                                      REPO, {"pr_number": 101, "kind": "control"})
    assert item["status"] == "skipped"
    call.assert_not_called()


def test_review_pr_marks_unparseable_output_as_error(retro_review):
    with patch.object(retro_review, "_call_review_model", return_value="I could not decide."):
        item = retro_review.review_pr(_client_with(_MERGED_PR, _FILES), REPO,
                                      {"pr_number": 101, "kind": "control"})
    assert item["status"] == "error"
    assert "verdict" not in item


def test_huge_diffs_are_truncated_like_the_live_review():
    huge = [{**_FILES[0], "patch": "+" + "x" * 500_000}]
    prompt = retro.build_retro_prompt(REPO, _MERGED_PR, huge, None)
    assert len(prompt) < 100_000
    assert "truncated" in prompt


def test_review_pr_isolates_one_failure(retro_review):
    client = MagicMock()
    client.get_pull_request.side_effect = RuntimeError("boom")
    item = retro_review.review_pr(client, REPO, {"pr_number": 101, "kind": "control"})
    assert item["status"] == "error"


def test_review_pr_stops_the_job_on_rate_limit(retro_review):
    client = MagicMock()
    client.get_pull_request.side_effect = GitHubRateLimited("limit")
    with pytest.raises(GitHubRateLimited):
        retro_review.review_pr(client, REPO, {"pr_number": 101, "kind": "control"})


_APP_FILES = [{"filename": "src/app.py", "status": "modified", "additions": 1, "deletions": 1,
               "patch": "@@ -1 +1 @@\n-x = 1\n+x = 2"}]


def test_review_pr_marks_app_only_prs_out_of_scope_without_the_model(retro_review):
    with patch.object(retro_review, "_call_review_model") as call:
        item = retro_review.review_pr(_client_with(_MERGED_PR, _APP_FILES), REPO,
                                      {"pr_number": 101, "kind": "reverted"})
    assert item["status"] == "out_of_scope"
    call.assert_not_called()


# ----------------------------------------------------------------------
# run_retro_review: candidate scanning fills each set with in-scope PRs
# ----------------------------------------------------------------------

def test_job_scans_past_out_of_scope_prs_to_fill_each_set(retro_review):
    infra_prs = {101, 103, 99}  # everything else only touches app code
    reverts = [_pr(200 + n, f'Revert "x (#{n})"') for n in (101, 102, 103, 104)]
    merged = [_pr(n, f"PR {n}") for n in (99, 98, 97)]

    client = MagicMock()
    client.search_merged_revert_prs.return_value = reverts
    client.list_recent_merged_prs.return_value = merged
    client.get_pull_request.side_effect = lambda n: {**_MERGED_PR, "number": n}
    client.list_files.side_effect = lambda n: _FILES if n in infra_prs else _APP_FILES
    client.get_diff.return_value = None

    updates = []
    with patch.object(retro_review, "PublicGitHubClient", return_value=client), \
         patch.object(retro_review, "_load_job",
                      return_value={"repo": REPO, "max_reverted": 2, "max_controls": 1}), \
         patch.object(retro_review, "_update_job", side_effect=lambda *a, **k: updates.append(k)), \
         patch.object(retro_review, "_call_review_model", return_value=_RISKY_JSON) as call:
        result = retro_review.run_retro_review(MagicMock(), "job", "user")

    assert result == {"status": "completed"}
    final = updates[-1]
    assert final["status"] == "completed"
    s = final["summary"]
    assert s["reverted_reviewed"] == 2 and s["caught"] == 2   # 101 and 103; 102 dropped; 104 never scanned
    assert s["controls_reviewed"] == 1                         # 99 fills the quota; 98, 97 never scanned
    assert s["out_of_scope"] == 1
    assert call.call_count == 3                                # the model only sees in-scope PRs
    scanned = [n for (n,) in (c.args for c in client.list_files.call_args_list)]
    assert scanned == [101, 102, 103, 99]


def test_job_fails_clearly_when_no_reverted_pr_touches_infra(retro_review):
    client = MagicMock()
    client.search_merged_revert_prs.return_value = [_pr(200, 'Revert "x (#101)"')]
    client.list_recent_merged_prs.return_value = []
    client.get_pull_request.return_value = _MERGED_PR
    client.list_files.return_value = _APP_FILES

    updates = []
    with patch.object(retro_review, "PublicGitHubClient", return_value=client), \
         patch.object(retro_review, "_load_job",
                      return_value={"repo": REPO, "max_reverted": 2, "max_controls": 1}), \
         patch.object(retro_review, "_update_job", side_effect=lambda *a, **k: updates.append(k)), \
         patch.object(retro_review, "_call_review_model") as call:
        result = retro_review.run_retro_review(MagicMock(), "job", "user")

    assert result == {"status": "no_infra_reverts"}
    assert updates[-1]["status"] == "failed"
    assert "infra" in updates[-1]["error"]
    call.assert_not_called()

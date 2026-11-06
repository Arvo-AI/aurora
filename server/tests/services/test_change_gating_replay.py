"""Revert replay: detection, case selection, prompt, scoring and the loop.

Fixture titles and bodies are shaped on real merged revert PRs (GitHub's
Revert button, ``Reverts #N``, markdown links, ``git revert`` bodies,
conventional-commit prefixes) so the rules are pinned to what teams write.
No network, no LLM, no Redis.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from services.change_gating import replay
from services.change_gating.replay import (
    ReplayCase,
    ReplayResult,
    build_replay_prompt,
    find_revert_target,
    headline,
    is_revert_title,
    render_markdown,
    run_replay,
    score,
    select_cases,
)

REPO = "acme/api"
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def _pr(number, title, *, body="", merged_at="2026-10-01T12:00:00Z", base="main",
        user_type="User", merge_sha=None):
    pr = {
        "number": number,
        "title": title,
        "body": body,
        "merged_at": merged_at,
        "base": {"ref": base, "sha": f"base{number}", "repo": {"full_name": REPO}},
        "head": {"sha": f"head{number}", "ref": f"branch-{number}"},
        "user": {"login": "dev", "type": user_type},
    }
    if merge_sha:
        pr["merge_commit_sha"] = merge_sha
    return pr


# ---------------------------------------------------------------------------
# is_revert_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    'Revert "CI: Build Go targets in groups"',
    'revert: "feat(dashboards): metric widgets replace the metrics insight tab"',
    "revert(frontend): remove the render app as update boot path",
    'fix(hogql): revert "export only the read keys of properties"',
    "PanelEditNext: Revert #125773",
    '[lts-active] Revert "Optimize strict module factory chunks (#98421)"',
    "Reverted the pool size change",
])
def test_revert_titles_are_recognised(title):
    assert is_revert_title(title)


@pytest.mark.parametrize("title", [
    "Prevent duplicate webhook deliveries",
    "Add irreversible migration guard",
    "feat: reverse proxy for the API",
    "",
    None,
])
def test_non_revert_titles_are_not(title):
    assert not is_revert_title(title)


# ---------------------------------------------------------------------------
# find_revert_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,expected", [
    ("Reverts acme/api#133705, as adblockers block the beacon route.", 133705),
    ("Reverts #127964. Since it reached production at 22:48 UTC today", 127964),
    ("## Related Issue\nReverts [#134677](https://github.com/acme/api/pull/134677)", 134677),
    ("This reverts https://github.com/acme/api/pull/99549 and #100597", 99549),
    ("Revert part of #95732\n\nThis would cause a lot of worker pools", 95732),
])
def test_reverts_reference_spellings(body, expected):
    revert = _pr(9000, 'Revert "something"', body=body)
    assert find_revert_target(revert, REPO) == (expected, "reverts_ref")


def test_reference_to_another_repo_is_ignored():
    revert = _pr(9000, 'Revert "bump"', body="Reverts other/repo#12")
    assert find_revert_target(revert, REPO) is None


def test_quoted_squash_title():
    revert = _pr(9000, 'Revert "Turbopack: loader change invalidation (#95732)"')
    assert find_revert_target(revert, REPO) == (95732, "quoted_squash")


def test_git_revert_sha_matches_merge_commit():
    revert = _pr(9000, 'Revert "Raise pool size"',
                 body="This reverts commit 1a2b3c4d5e6f7a8b9c0d.")
    found = find_revert_target(revert, REPO, by_merge_sha={"1a2b3c4d5e6f7a8b9c0d1111": 42})
    assert found == (42, "reverts_sha")


def test_quoted_title_matches_a_merged_pr():
    revert = _pr(9000, 'Revert "Raise pool size"')
    found = find_revert_target(revert, REPO, by_title={"Raise pool size": 42})
    assert found == (42, "quoted_title")


def test_single_reference_in_a_revert_body():
    revert = _pr(
        9000, "revert(frontend): remove the render app as update boot path",
        body="[#119225](https://github.com/acme/api/pull/119225) added a boot path. It broke login.",
    )
    assert find_revert_target(revert, REPO) == (119225, "single_ref")


def test_ambiguous_references_do_not_match():
    revert = _pr(9000, "fix: revert flaky changes", body="Touches #1 and #2.")
    assert find_revert_target(revert, REPO) is None


def test_a_revert_never_targets_itself():
    revert = _pr(9000, 'Revert "x"', body="Reverts #9000")
    assert find_revert_target(revert, REPO) is None


# ---------------------------------------------------------------------------
# select_cases
# ---------------------------------------------------------------------------


def _fetcher(prs):
    calls = []

    def fetch(number):
        calls.append(number)
        return prs.get(number)

    fetch.calls = calls
    return fetch


def test_selects_reverted_targets_and_old_human_controls():
    culprit = _pr(10, "Lower DB pool to 10", merged_at="2026-10-30T10:00:00Z")
    reverts = [_pr(20, 'Revert "Lower DB pool to 10"', body="Reverts #10\n\nCheckout timed out at lunch.")]
    pool = [
        _pr(31, "Too fresh to be a control", merged_at="2026-11-04T00:00:00Z"),
        _pr(32, "Bump deps", merged_at="2026-10-20T00:00:00Z", user_type="Bot"),
        _pr(10, "Lower DB pool to 10", merged_at="2026-10-28T00:00:00Z"),
        _pr(33, "Add retry budget", merged_at="2026-10-20T00:00:00Z"),
        _pr(34, "Release branch fix", merged_at="2026-10-20T00:00:00Z", base="release/1.2"),
    ]
    cases = select_cases(reverts, pool, REPO, "main", fetch_pull=_fetcher({10: culprit}), now=NOW)

    assert [(c.pr_number, c.label) for c in cases] == [(10, "reverted"), (33, "control")]
    first = cases[0]
    assert first.revert_pr_number == 20
    assert first.match_rule == "reverts_ref"
    assert "Checkout timed out" in first.revert_reason


def test_relands_and_unmerged_targets_are_skipped():
    reland_target = _pr(11, 'Revert "Add cache"')  # a revert that was itself reverted
    unmerged = _pr(12, "Never merged", merged_at=None)
    off_branch = _pr(13, "Backport", base="release/1.2")
    reverts = [
        _pr(21, 'Revert "Revert "Add cache""', body="Reverts #11"),
        _pr(22, 'Revert "Never merged"', body="Reverts #12"),
        _pr(23, 'Revert "Backport"', body="Reverts #13"),
    ]
    fetch = _fetcher({11: reland_target, 12: unmerged, 13: off_branch})
    assert select_cases(reverts, [], REPO, "main", fetch_pull=fetch, now=NOW) == []


def test_a_reverted_pr_is_never_a_control_even_past_the_limit():
    reverts = [
        _pr(21, 'Revert "A"', body="Reverts #1"),
        _pr(22, 'Revert "B"', body="Reverts #2"),
    ]
    pool = [_pr(2, "B", merged_at="2026-10-01T00:00:00Z"), _pr(3, "C", merged_at="2026-10-01T00:00:00Z")]
    fetch = _fetcher({1: _pr(1, "A"), 2: _pr(2, "B")})
    cases = select_cases(reverts, pool, REPO, "main", fetch_pull=fetch,
                         reverted_limit=1, control_limit=5, now=NOW)

    assert [(c.pr_number, c.label) for c in cases] == [(1, "reverted"), (3, "control")]
    assert fetch.calls == [1]  # stops fetching once the reverted set is full


def test_duplicate_reverts_of_one_pr_count_once():
    reverts = [
        _pr(21, 'Revert "A"', body="Reverts #1"),
        _pr(22, '[lts] Revert "A"', body="Reverts #1 on the release branch"),
    ]
    cases = select_cases(reverts, [], REPO, "main", fetch_pull=_fetcher({1: _pr(1, "A")}), now=NOW)
    assert [c.pr_number for c in cases] == [1]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def test_replay_prompt_pins_the_review_before_merge_and_hides_the_label():
    pr = _pr(10, "Lower DB pool to 10", merged_at="2026-10-30T10:00:00Z")
    files = [{"filename": "config/db.yaml", "status": "modified", "additions": 1,
              "deletions": 1, "patch": "@@ -1 +1 @@\n-pool_size: 50\n+pool_size: 10"}]
    prompt = build_replay_prompt(REPO, pr, files, None)

    note = prompt.index("HISTORICAL REPLAY:")
    assert note < prompt.index("PR METADATA:")
    assert "merged on 2026-10-30T10:00:00Z" in prompt
    assert "base commit\nbase10." in prompt
    assert "pool_size: 10" in prompt
    # Reverted and control PRs must get byte-identical prompts.
    assert "reverted" not in prompt.lower()
    assert "NO tools" not in prompt


def test_diff_only_mode_says_there_are_no_tools():
    prompt = build_replay_prompt(REPO, _pr(10, "x"), [], "diff --git a/x b/x", tools_available=False)
    assert "you have NO tools" in prompt


# ---------------------------------------------------------------------------
# Scoring and report
# ---------------------------------------------------------------------------


def _result(label, verdict, status="completed", number=1):
    case = ReplayCase(pr_number=number, title=f"PR {number}", label=label,
                      revert_pr_number=900 + number if label == "reverted" else None,
                      revert_reason="Broke checkout." if label == "reverted" else None)
    findings = [{"severity": "HIGH", "title": "Pool exhausted at peak"}] if verdict == "RISKY" else []
    return ReplayResult(case=case, status=status, verdict=verdict if status == "completed" else None,
                        findings=findings, error=None if status == "completed" else "agent_failed")


def test_score_counts_catches_false_alarms_and_leaves_errors_out():
    results = [
        _result("reverted", "RISKY", number=1),
        _result("reverted", "SAFE", number=2),
        _result("reverted", None, status="error", number=3),
        _result("control", "SAFE", number=4),
        _result("control", "RISKY", number=5),
    ]
    s = score(results)
    assert s == {
        "reverted_reviewed": 2, "reverted_flagged": 1,
        "controls_reviewed": 2, "controls_flagged": 1,
        "errors": 1, "recall": 0.5, "false_alarm_rate": 0.5,
    }
    assert headline(REPO, s) == (
        "Aurora would have flagged 1 of your last 2 reverted PRs before merge, "
        "and 1 of 2 PRs that shipped fine."
    )


def test_headline_without_reverts():
    s = score([])
    assert "No reverted pull requests found" in headline(REPO, s)


def test_markdown_report_shows_the_revert_reason_next_to_the_verdict():
    report = replay.build_report(REPO, [_result("reverted", "RISKY"), _result("control", "SAFE", number=2)])
    md = render_markdown(report)
    assert "| #1 PR 1 | #901 | **RISKY** | Pool exhausted at peak | Broke checkout. |" in md
    assert "### Control PRs" in md
    assert "posts nothing to GitHub" in md
    json.dumps(report)  # the report is stored in Redis as JSON


# ---------------------------------------------------------------------------
# run_replay
# ---------------------------------------------------------------------------


class FakeReader:
    def __init__(self):
        self.prs = {
            1: _pr(1, "Lower DB pool to 10"),
            2: _pr(2, "Drop the readiness probe"),
            3: _pr(3, "Add retry budget", merged_at="2026-10-01T00:00:00Z"),
        }
        self.searches = []

    def get_repo(self):
        return {"default_branch": "main"}

    def search_merged_pulls(self, query="", limit=100):
        self.searches.append(query)
        if "revert" in query:
            return [_pr(21, 'Revert "Lower DB pool to 10"', body="Reverts #1"),
                    _pr(22, 'Revert "Drop the readiness probe"', body="Reverts #2")]
        return [self.prs[3]]

    def find_pull_request(self, number):
        return self.prs.get(number)

    def list_files(self, number):
        return [{"filename": f"f{number}.yaml", "status": "modified", "additions": 1,
                 "deletions": 1, "patch": "@@ -1 +1 @@\n-a\n+b"}]

    def get_diff(self, number):
        return None


def test_run_replay_reviews_reverted_first_and_survives_one_failure():
    reader = FakeReader()
    progress = []

    def review(pr, files, diff):
        if pr["number"] == 2:
            raise RuntimeError("agent_failed")
        return {"verdict": "RISKY" if pr["number"] == 1 else "SAFE", "summary": "", "findings": []}

    report = run_replay(reader, REPO, review=review, on_progress=progress.append)

    assert [r["case"]["pr_number"] for r in report["results"]] == [1, 2, 3]
    assert [r["status"] for r in report["results"]] == ["completed", "error", "completed"]
    assert report["status"] == "completed"
    assert report["scores"]["reverted_flagged"] == 1
    assert report["scores"]["errors"] == 1
    assert [p["status"] for p in progress] == ["running"] * 3
    assert progress[0]["total_cases"] == 3
    # Controls are searched on the default branch, at least a week old.
    assert any(q.startswith("base:main merged:<=") for q in reader.searches)


# ---------------------------------------------------------------------------
# PublicRepoReader
# ---------------------------------------------------------------------------


def test_public_reader_never_writes_and_sends_no_auth_without_a_token():
    from services.change_gating import github_adapter

    reader = github_adapter.PublicRepoReader(REPO)
    assert "Authorization" not in reader._headers()
    for method in ("post_review", "post_issue_comment", "delete_issue_comment",
                   "dismiss_review", "update_review_body", "supersede_review"):
        with pytest.raises(PermissionError):
            getattr(reader, method)(1, "x")
    assert github_adapter.PublicRepoReader(REPO, token="t")._headers()["Authorization"] == "Bearer t"

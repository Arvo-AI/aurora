"""Unit tests for the nightly ``risk_outcomes`` linker.

The DB-touching path is integration-only (requires Postgres + RLS).
Here we pin the pure-function regex/extraction logic that drives the
matcher — false positives or missed URL formats translate directly
into wrong / missing risk_outcomes rows downstream, so the regex
needs the same scrutiny as the validator.
"""

from __future__ import annotations

import pytest

from services.change_intercept.linker import extract_pr_references_from_text


# ─── Happy-path extraction ──────────────────────────────────────────


def test_extract_canonical_https_url() -> None:
    refs = extract_pr_references_from_text(
        "See https://github.com/acme/widgets/pull/42 for details"
    )
    assert len(refs) == 1
    assert refs[0]["owner"] == "acme"
    assert refs[0]["repo"] == "widgets"
    assert refs[0]["num"] == "42"
    assert refs[0]["dedup_key"] == "github:acme/widgets:42"


def test_extract_naked_github_dot_com() -> None:
    refs = extract_pr_references_from_text(
        "Affected by github.com/foo/bar/pull/100"
    )
    assert refs[0]["dedup_key"] == "github:foo/bar:100"


def test_extract_http_scheme() -> None:
    refs = extract_pr_references_from_text(
        "Old link: http://github.com/foo/bar/pull/5"
    )
    assert refs[0]["dedup_key"] == "github:foo/bar:5"


def test_extract_with_www_prefix() -> None:
    refs = extract_pr_references_from_text(
        "https://www.github.com/foo/bar/pull/99"
    )
    assert refs[0]["dedup_key"] == "github:foo/bar:99"


def test_extract_with_trailing_path_segments() -> None:
    refs = extract_pr_references_from_text(
        "https://github.com/foo/bar/pull/42/files"
    )
    assert refs[0]["dedup_key"] == "github:foo/bar:42"


def test_extract_with_trailing_query_and_anchor() -> None:
    refs = extract_pr_references_from_text(
        "https://github.com/foo/bar/pull/7?diff=1#issuecomment-100"
    )
    assert refs[0]["dedup_key"] == "github:foo/bar:7"


def test_extract_handles_hyphens_and_dots_in_repo() -> None:
    refs = extract_pr_references_from_text(
        "https://github.com/some-org/my.repo-name/pull/3"
    )
    assert refs[0]["dedup_key"] == "github:some-org/my.repo-name:3"


def test_extract_handles_underscores_in_owner() -> None:
    refs = extract_pr_references_from_text(
        "https://github.com/my_team/widgets/pull/9"
    )
    assert refs[0]["dedup_key"] == "github:my_team/widgets:9"


def test_extract_multiple_distinct_refs() -> None:
    text = """
    Possibly caused by https://github.com/acme/widgets/pull/42.
    Cross-reference also: https://github.com/acme/widgets/pull/43
    And the rollback: https://github.com/acme/widgets/pull/44
    """
    refs = extract_pr_references_from_text(text)
    keys = [r["dedup_key"] for r in refs]
    assert keys == [
        "github:acme/widgets:42",
        "github:acme/widgets:43",
        "github:acme/widgets:44",
    ]


# ─── Dedup ──────────────────────────────────────────────────────────


def test_extract_deduplicates_repeated_url() -> None:
    refs = extract_pr_references_from_text(
        "https://github.com/foo/bar/pull/42 and again https://github.com/foo/bar/pull/42"
    )
    assert len(refs) == 1


def test_extract_dedup_preserves_first_occurrence_order() -> None:
    text = "B at github.com/o/r2/pull/2 then A at github.com/o/r1/pull/1 then B again"
    refs = extract_pr_references_from_text(text)
    keys = [r["dedup_key"] for r in refs]
    assert keys == ["github:o/r2:2", "github:o/r1:1"]


# ─── Anti-cases ─────────────────────────────────────────────────────


def test_extract_returns_empty_on_no_match() -> None:
    assert extract_pr_references_from_text("nothing here") == []


def test_extract_returns_empty_on_empty_input() -> None:
    assert extract_pr_references_from_text("") == []
    assert extract_pr_references_from_text(None) == []  # type: ignore[arg-type]


def test_extract_does_not_match_issues_urls() -> None:
    # Aurora's gate is PRs only; issue URLs must not produce a ref.
    assert (
        extract_pr_references_from_text("https://github.com/foo/bar/issues/42")
        == []
    )


def test_extract_does_not_match_other_hosts() -> None:
    # Avoid GitLab / Bitbucket false positives — they have their own
    # adapters and dedup_key formats.
    assert (
        extract_pr_references_from_text(
            "https://gitlab.com/foo/bar/-/merge_requests/42"
        )
        == []
    )
    assert (
        extract_pr_references_from_text(
            "https://bitbucket.org/foo/bar/pull-requests/42"
        )
        == []
    )


def test_extract_does_not_match_non_numeric_pr_id() -> None:
    assert (
        extract_pr_references_from_text("https://github.com/foo/bar/pull/abc")
        == []
    )


def test_extract_is_case_insensitive_for_host() -> None:
    refs = extract_pr_references_from_text("https://GitHub.com/foo/bar/pull/42")
    assert refs and refs[0]["dedup_key"] == "github:foo/bar:42"

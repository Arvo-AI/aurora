"""Unit tests for the investigator prompt builder.

The prompt is what determines verdict-distribution shape during the
calibration phase. Regressions in the prompt template silently skew
the dry-run severity histogram, so we pin the structural sections
(mission, taxonomy, output schema, snapshot, guidance) here.
"""

from __future__ import annotations

from typing import Any

from services.change_intercept.prompts import (
    build_followup_prompt,
    build_initial_prompt,
)
from services.change_intercept.risk_taxonomy import CATEGORIES


def _snapshot(
    *,
    body: str = "Tighten retry logic per incident-1234.",
    diff: str = "diff --git a/foo.py b/foo.py\n+resp = requests.get(url)\n",
    files: list[dict[str, Any]] | None = None,
    commits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "change_body": body,
        "change_diff": diff,
        "change_files": files
        or [{"path": "foo.py", "status": "modified", "additions": 2, "deletions": 0}],
        "change_commits": commits
        or [{"sha": "abc1234567", "message": "tighten retry", "author": "alice"}],
    }


def _event_meta(
    *,
    repo: str = "acme/widgets",
    ref: str = "feat/retry",
    base_ref: str = "main",
    commit_sha: str = "deadbeef12345678",
    actor: str = "alice",
    target_env: str = "prod",
) -> dict[str, Any]:
    return {
        "repo": repo,
        "ref": ref,
        "base_ref": base_ref,
        "commit_sha": commit_sha,
        "actor": actor,
        "target_env": target_env,
    }


# ─── Sections present ───────────────────────────────────────────────


def test_initial_prompt_contains_mission_block() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    # The mission paragraph wraps across lines, so normalise whitespace
    # before substring checks. Pin meaningful phrases, not their exact
    # in-template line break positions.
    normalised = " ".join(p.split())
    assert "SRE-focused PR reviewer" in normalised
    assert "production incident" in normalised
    assert "Do NOT flag style" in normalised


def test_initial_prompt_contains_all_taxonomy_slugs() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    for slug in CATEGORIES:
        assert slug in p, f"prompt missing taxonomy slug {slug!r}"


def test_initial_prompt_contains_output_schema_section() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    assert '"verdict"' in p
    assert '"severity"' in p
    assert '"confidence"' in p
    assert '"findings"' in p
    assert "[diff]" in p


def test_initial_prompt_contains_snapshot_metadata() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    assert "acme/widgets" in p
    assert "feat/retry" in p
    assert "prod" in p
    # Short SHA truncated to 12 chars in the prompt.
    assert "deadbeef1234" in p


def test_initial_prompt_renders_diff_in_fenced_block() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    assert "```diff" in p
    assert "requests.get" in p


def test_initial_prompt_renders_files_block() -> None:
    p = build_initial_prompt(
        _snapshot(
            files=[
                {"path": "foo.py", "status": "modified", "additions": 2, "deletions": 0},
                {"path": "bar.py", "status": "added", "additions": 100, "deletions": 0},
            ]
        ),
        _event_meta(),
    )
    assert "foo.py" in p and "bar.py" in p
    assert "+2 / -0" in p
    assert "+100 / -0" in p


def test_initial_prompt_renders_commit_messages_with_author() -> None:
    p = build_initial_prompt(
        _snapshot(
            commits=[
                {"sha": "abc12345xx", "message": "tighten retry", "author": "alice"}
            ]
        ),
        _event_meta(),
    )
    assert "tighten retry" in p
    assert "`alice`" in p


def test_initial_prompt_truncates_long_diff_with_marker() -> None:
    big = "+x\n" * 100_000  # ~300KB
    p = build_initial_prompt(
        _snapshot(diff=big), _event_meta()
    )
    assert "[truncated" in p


def test_initial_prompt_handles_missing_body_gracefully() -> None:
    p = build_initial_prompt(_snapshot(body=""), _event_meta())
    assert "no PR body provided" in p


def test_initial_prompt_handles_unknown_meta_gracefully() -> None:
    # All meta fields None — prompt should still render with placeholders.
    p = build_initial_prompt(
        _snapshot(),
        {
            "repo": None,
            "ref": None,
            "base_ref": None,
            "commit_sha": None,
            "actor": None,
            "target_env": None,
        },
    )
    assert "(unknown repo)" in p
    # commit_sha falls through the [:12] truncation; placeholder
    # surfaces as the leading "(unknown sha" with the trailing paren
    # clipped. The exact substring is not load-bearing — assert on
    # the unambiguous "(unknown" prefix.
    assert "(unknown sha" in p


# ─── Operating guidance ─────────────────────────────────────────────


def test_initial_prompt_includes_soft_depth_guidance() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    assert "Be tight" in p
    assert "0-3 findings" in p


def test_initial_prompt_explains_high_confidence_bar() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    assert "HIGH" in p
    assert "MEDIUM" in p
    assert "LOW" in p


def test_initial_prompt_forbids_prose_around_json() -> None:
    p = build_initial_prompt(_snapshot(), _event_meta())
    assert "ONLY a single JSON object" in p
    # Normalise newlines that wrap the "No prose" phrase across lines.
    normalised = " ".join(p.split())
    assert "No prose" in normalised


# ─── Followup variant ───────────────────────────────────────────────


def test_followup_prompt_includes_prior_verdict_and_summary() -> None:
    p = build_followup_prompt(
        snapshot=_snapshot(),
        event_meta=_event_meta(),
        prior_investigation={
            "verdict": "request_changes",
            "summary": "we previously flagged X",
            "findings": [
                {
                    "severity": "HIGH",
                    "category": "missing_timeout",
                    "file_path": "foo.py",
                    "start_line": 11,
                    "title": "no timeout",
                }
            ],
        },
        followup_comment="i fixed the timeout, see line 11",
    )
    assert "Your previous assessment" in p
    assert "request_changes" in p
    assert "we previously flagged X" in p
    assert "no timeout" in p
    assert "i fixed the timeout" in p


def test_followup_prompt_with_empty_reply_renders_placeholder() -> None:
    p = build_followup_prompt(
        snapshot=_snapshot(),
        event_meta=_event_meta(),
        prior_investigation={"verdict": "approve", "summary": "x", "findings": []},
        followup_comment="",
    )
    assert "(empty reply)" in p


def test_followup_prompt_instructs_on_revising_findings() -> None:
    p = build_followup_prompt(
        snapshot=_snapshot(),
        event_meta=_event_meta(),
        prior_investigation={"verdict": "approve", "summary": "x", "findings": []},
        followup_comment="thoughts?",
    )
    assert "Re-evaluate the PR" in p
    assert "drop, or add findings" in p

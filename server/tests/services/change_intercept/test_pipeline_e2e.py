"""End-to-end pipeline tests.

These chain the four module surfaces (adapter → prompts → validator →
review poster) together against synthetic webhook payloads and mocked
LLM responses. The goal is to catch integration bugs that pin-pointed
unit tests would miss — wire mismatches between the dataclass fields,
silent-drop paths that swallow real findings, off-by-one mismatches
between the diff parser and the prompt's line numbers, etc.

The launch_investigation Celery task itself is mocked at the LLM /
DB boundary; we don't exercise the actual celery worker or postgres
here (those are integration-test territory). What we DO exercise is
the data flow from webhook payload through to the final
``RenderedReview`` object the adapter would post.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.change_intercept.adapters.github import GitHubChangeAdapter
from services.change_intercept.pr_review_poster import render_review
from services.change_intercept.prompts import build_initial_prompt
from services.change_intercept.verdict_validator import validate


@pytest.fixture
def pr_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "installation": {"id": 99999},
        "pull_request": {
            "number": 42,
            "draft": False,
            "head": {"ref": "feat/foo", "sha": "deadbeef"},
            "base": {"ref": "main"},
            "user": {"login": "alice"},
        },
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "alice"},
    }


@pytest.fixture
def sample_diff() -> str:
    return (
        "diff --git a/server/services/foo.py b/server/services/foo.py\n"
        "--- a/server/services/foo.py\n"
        "+++ b/server/services/foo.py\n"
        "@@ -10,3 +10,5 @@ def handle():\n"
        " ctx\n"
        "-old_call(url)\n"
        "+resp = requests.get(url)\n"
        "+for _ in range(retries):\n"
        "+    process(resp)\n"
        " ctx\n"
    )


def test_pipeline_high_high_finding_flows_to_inline_comment(
    pr_payload: dict[str, Any], sample_diff: str
) -> None:
    """A clean HIGH+HIGH finding from the LLM should land as exactly
    one inline comment with the expected severity tag + path:line."""
    event = GitHubChangeAdapter().parse(
        "pull_request", pr_payload, org_id="org-1"
    )
    assert event is not None

    investigator_output = {
        "verdict": "request_changes",
        "summary": "HTTP call without timeout will hang the request thread.",
        "intent_alignment": "matches",
        "intent_notes": None,
        "findings": [
            {
                "severity": "HIGH",
                "confidence": "HIGH",
                "category": "missing_timeout",
                "file_path": "server/services/foo.py",
                "start_line": 11,
                "end_line": None,
                "title": "HTTP call without timeout",
                "rationale": (
                    "requests.get on line 11 has no timeout kwarg. [diff]"
                ),
                "cited_tool_calls": [],
            }
        ],
    }

    validation = validate(investigator_output, sample_diff)
    rendered = render_review(validation)

    assert validation.verdict == "request_changes"
    assert len(validation.findings) == 1
    assert validation.findings[0].will_post_inline
    assert rendered.verdict_event == "REQUEST_CHANGES"
    assert len(rendered.inline_comments) == 1
    assert rendered.inline_comments[0]["start_line"] == 11
    assert rendered.inline_comments[0]["path"] == "server/services/foo.py"
    assert "[HIGH]" in rendered.inline_comments[0]["body"]
    assert "Missing timeout" in rendered.inline_comments[0]["body"]


def test_pipeline_hallucinated_finding_dropped_and_downgrades_verdict(
    pr_payload: dict[str, Any], sample_diff: str
) -> None:
    """LLM cites a file the diff doesn't touch — validator drops it,
    verdict reconciles to approve, no inline comments emitted."""
    investigator_output = {
        "verdict": "request_changes",
        "summary": "I think there might be issues.",
        "intent_alignment": "matches",
        "intent_notes": None,
        "findings": [
            {
                "severity": "HIGH",
                "confidence": "HIGH",
                "category": "missing_timeout",
                "file_path": "server/services/other.py",  # not in diff
                "start_line": 99,
                "title": "Imaginary issue",
                "rationale": "Probably bad [diff]",
                "cited_tool_calls": [],
            }
        ],
    }

    validation = validate(investigator_output, sample_diff)
    rendered = render_review(validation)

    assert validation.verdict == "approve"
    assert validation.downgraded_to_approve is True
    assert validation.findings == []
    assert rendered.verdict_event == "APPROVE"
    assert rendered.inline_comments == []
    # The body carries the downgrade breadcrumb so calibration readers
    # can tell apart "no findings at all" from "all findings dropped."
    assert "investigator flagged this PR" in rendered.body


def test_pipeline_mixed_severities_render_to_distinct_sections(
    pr_payload: dict[str, Any], sample_diff: str
) -> None:
    """HIGH inline-only, MEDIUM in body, LOW under Other notes — verifies
    the renderer respects severity → section mapping end-to-end."""
    investigator_output = {
        "verdict": "request_changes",
        "summary": "Three notes on this PR.",
        "intent_alignment": "matches",
        "intent_notes": None,
        "findings": [
            {
                "severity": "HIGH",
                "confidence": "HIGH",
                "category": "missing_timeout",
                "file_path": "server/services/foo.py",
                "start_line": 11,
                "title": "no timeout",
                "rationale": "see line 11 [diff]",
                "cited_tool_calls": [],
            },
            {
                "severity": "MEDIUM",
                "confidence": "HIGH",
                "category": "unbounded_retry",
                "file_path": "server/services/foo.py",
                "start_line": 12,
                "title": "retry loop has no break",
                "rationale": "while loop [diff]",
                "cited_tool_calls": [],
            },
            {
                "severity": "LOW",
                "confidence": "HIGH",
                "category": "n_plus_one",
                "file_path": "server/services/foo.py",
                "start_line": 13,
                "title": "minor loop",
                "rationale": "small risk [diff]",
                "cited_tool_calls": [],
            },
        ],
    }

    validation = validate(investigator_output, sample_diff)
    rendered = render_review(validation)

    assert validation.verdict == "request_changes"
    assert len(rendered.inline_comments) == 1
    assert "### Risks identified" in rendered.body
    assert "### Other notes" in rendered.body
    risks_idx = rendered.body.index("### Risks identified")
    other_idx = rendered.body.index("### Other notes")
    assert risks_idx < other_idx


def test_pipeline_synchronize_event_parses_but_could_be_skipped(
    pr_payload: dict[str, Any]
) -> None:
    """Synchronize parses to a code_change event — the dispatcher's
    _should_enqueue_investigation policy is what skips it, not the
    adapter or validator. Verify the data still flows."""
    pr_payload["action"] = "synchronize"
    event = GitHubChangeAdapter().parse(
        "pull_request", pr_payload, org_id="org-1"
    )
    assert event is not None
    assert event.action == "synchronize"
    assert event.kind == "code_change"


def test_pipeline_prompt_includes_diff_metadata_for_investigator(
    sample_diff: str,
) -> None:
    """The prompt the investigator receives must contain the diff text
    and the PR's repo / actor metadata. Pin this so a regression that
    truncates or omits these can't sneak in unnoticed."""
    snapshot = {
        "change_body": "Fix retry handling.",
        "change_diff": sample_diff,
        "change_files": [
            {
                "path": "server/services/foo.py",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
            }
        ],
        "change_commits": [
            {"sha": "deadbeef00", "message": "fix retry", "author": "alice"}
        ],
    }
    meta = {
        "repo": "acme/widgets",
        "ref": "feat/foo",
        "base_ref": "main",
        "commit_sha": "deadbeef00",
        "actor": "alice",
        "target_env": "prod",
    }
    prompt = build_initial_prompt(snapshot, meta)

    # Snapshot identifiers present.
    assert "acme/widgets" in prompt
    assert "alice" in prompt
    # Diff content is reproduced verbatim inside the fenced block.
    assert "requests.get(url)" in prompt
    # Every category slug is enumerated so the investigator can pick one.
    from services.change_intercept.risk_taxonomy import CATEGORIES

    for slug in CATEGORIES:
        assert slug in prompt


def test_pipeline_at_mention_followup_carries_engineer_reply(
    pr_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``@aurora-test`` comment surfaces as a code_change_followup
    with the engineer's reply text intact, so the followup prompt
    builder can render it back to the LLM."""
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    comment_payload = {
        "action": "created",
        "installation": {"id": 99999},
        "issue": {
            "number": 42,
            "pull_request": {"url": "https://example/acme/widgets/pulls/42"},
        },
        "comment": {
            "id": 5555,
            "body": "@aurora-test re-review, fixed the timeout on line 11",
            "user": {"login": "alice"},
            "in_reply_to_id": None,
        },
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "alice"},
    }
    event = GitHubChangeAdapter().parse(
        "issue_comment", comment_payload, org_id="org-1"
    )
    assert event is not None
    assert event.kind == "code_change_followup"
    assert event.parent_external_id == "42"
    assert "fixed the timeout on line 11" in (event.follow_up_comment or "")


def test_pipeline_findings_persist_to_json_roundtrip(sample_diff: str) -> None:
    """ValidatedFinding objects must round-trip through the JSONB
    serialisation the Celery task uses. A regression here would
    silently corrupt change_investigations.findings on insert."""
    raw = {
        "verdict": "request_changes",
        "summary": "x",
        "intent_alignment": "matches",
        "intent_notes": None,
        "findings": [
            {
                "severity": "HIGH",
                "confidence": "HIGH",
                "category": "missing_timeout",
                "file_path": "server/services/foo.py",
                "start_line": 11,
                "end_line": 12,
                "title": "T",
                "rationale": "[diff]",
                "cited_tool_calls": [
                    {"tool": "incident_feedback", "call_id": "x", "summary": "y"}
                ],
            }
        ],
    }
    validation = validate(raw, sample_diff)
    payload = [
        {
            "severity": f.severity,
            "confidence": f.confidence,
            "category": f.category,
            "file_path": f.file_path,
            "start_line": f.start_line,
            "end_line": f.end_line,
            "title": f.title,
            "rationale": f.rationale,
            "cited_tool_calls": list(f.cited_tool_calls),
            "will_post_inline": f.will_post_inline,
        }
        for f in validation.findings
    ]
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded[0]["severity"] == "HIGH"
    assert decoded[0]["end_line"] == 12
    assert decoded[0]["cited_tool_calls"][0]["tool"] == "incident_feedback"

"""Unit tests for the GitHub change-intercept adapter.

Covers the four methods wired in Part 1 (``verify_signature``,
``parse``, ``is_reply_to_us``, ``fetch_snapshot``) against synthetic
webhook payloads that mirror GitHub's actual shapes. Outbound HTTP is
stubbed by patching the module-level ``_get`` / ``_paginated_get``
helpers so the tests stay hermetic.

The adapter's signature-verification branch piggybacks on the existing
``verify_webhook_signature`` util that already has its own dedicated
test file — we only verify the delegation here, not the HMAC math.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from services.change_intercept.adapters import github as adapter_module
from services.change_intercept.adapters.base import (
    ChangeSnapshot,
    NormalizedChangeEvent,
    ReplyMatch,
)
from services.change_intercept.adapters.github import GitHubChangeAdapter


# ─── Helpers ─────────────────────────────────────────────────────────


def _adapter() -> GitHubChangeAdapter:
    return GitHubChangeAdapter()


def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = 42,
    repo: str = "acme/widgets",
    head_ref: str = "feat/foo",
    base_ref: str = "main",
    head_sha: str = "deadbeef",
    author: str = "alice",
    draft: bool = False,
    installation_id: int | None = 99999,
) -> dict[str, Any]:
    payload = {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "draft": draft,
            "head": {"ref": head_ref, "sha": head_sha},
            "base": {"ref": base_ref},
            "user": {"login": author},
        },
        "repository": {"full_name": repo},
        "sender": {"login": author},
    }
    if installation_id is not None:
        payload["installation"] = {"id": installation_id}
    return payload


def _comment_payload(
    *,
    event_type: str,
    pr_number: int = 42,
    repo: str = "acme/widgets",
    body: str = "regular comment",
    sender: str = "bob",
    in_reply_to_id: int | None = None,
    comment_id: int = 1234,
    installation_id: int = 99999,
) -> dict[str, Any]:
    if event_type == "issue_comment":
        return {
            "action": "created",
            "installation": {"id": installation_id},
            "issue": {
                "number": pr_number,
                "pull_request": {"url": f"https://example/{repo}/pulls/{pr_number}"},
            },
            "comment": {
                "id": comment_id,
                "body": body,
                "user": {"login": sender},
                "in_reply_to_id": in_reply_to_id,
            },
            "repository": {"full_name": repo},
            "sender": {"login": sender},
        }
    return {
        "action": "created",
        "installation": {"id": installation_id},
        "pull_request": {"number": pr_number},
        "comment": {
            "id": comment_id,
            "body": body,
            "user": {"login": sender},
            "in_reply_to_id": in_reply_to_id,
            "commit_id": "feedbeef",
        },
        "repository": {"full_name": repo},
        "sender": {"login": sender},
    }


# ─── parse(pull_request) ─────────────────────────────────────────────


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "synchronize"])
def test_parse_pr_accepts_supported_actions(action: str) -> None:
    ev = _adapter().parse("pull_request", _pr_payload(action=action), org_id="org-1")
    assert ev is not None
    assert ev.kind == "code_change"
    assert ev.action == action
    assert ev.dedup_key == "github:acme/widgets:42"


@pytest.mark.parametrize(
    "action",
    ["closed", "edited", "labeled", "review_requested", "converted_to_draft"],
)
def test_parse_pr_ignores_non_actionable_actions(action: str) -> None:
    assert _adapter().parse("pull_request", _pr_payload(action=action), org_id="org-1") is None


def test_parse_pr_ignores_draft_open() -> None:
    # We don't review drafts on open; we wait for ``ready_for_review``.
    assert (
        _adapter().parse("pull_request", _pr_payload(action="opened", draft=True), org_id="org-1")
        is None
    )


def test_parse_pr_returns_event_for_ready_for_review() -> None:
    # The same draft on ``ready_for_review`` (now draft=False) does fire.
    ev = _adapter().parse(
        "pull_request",
        _pr_payload(action="ready_for_review", draft=False),
        org_id="org-1",
    )
    assert ev is not None and ev.action == "ready_for_review"


def test_parse_pr_marks_target_env_prod_for_main_base() -> None:
    ev = _adapter().parse(
        "pull_request", _pr_payload(base_ref="main"), org_id="org-1"
    )
    assert ev is not None and ev.target_env == "prod"


def test_parse_pr_marks_target_env_non_prod_for_feature_base() -> None:
    ev = _adapter().parse(
        "pull_request", _pr_payload(base_ref="staging"), org_id="org-1"
    )
    assert ev is not None and ev.target_env == "non-prod"


def test_parse_pr_carries_installation_id_when_present() -> None:
    ev = _adapter().parse(
        "pull_request", _pr_payload(installation_id=12345), org_id="org-1"
    )
    assert ev is not None and ev.installation_id == 12345


def test_parse_pr_returns_none_when_pr_number_missing() -> None:
    payload = _pr_payload()
    # Corrupt the payload — pr_number is required.
    payload["pull_request"].pop("number")
    assert _adapter().parse("pull_request", payload, org_id="org-1") is None


def test_parse_pr_returns_none_when_repo_missing() -> None:
    payload = _pr_payload()
    payload.pop("repository")
    assert _adapter().parse("pull_request", payload, org_id="org-1") is None


# ─── parse(comment) → followup ───────────────────────────────────────


def test_parse_issue_comment_at_mention_makes_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="issue_comment",
        body="hey @aurora-test what about this part?",
    )
    ev = _adapter().parse("issue_comment", payload, org_id="org-1")
    assert ev is not None
    assert ev.kind == "code_change_followup"
    assert ev.action == "reply"
    assert ev.follow_up_comment == "hey @aurora-test what about this part?"
    assert ev.parent_external_id == "42"
    assert ev.external_id == "comment:1234"


def test_parse_threaded_review_comment_requires_aurora_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threaded replies only count when the parent comment was authored
    by Aurora. The embedded ``in_reply_to.user.login`` short-circuits
    the DB lookup."""
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="pull_request_review_comment",
        body="no I disagree",
        in_reply_to_id=999,
    )
    payload["comment"]["in_reply_to"] = {"user": {"login": "aurora-test[bot]"}}
    ev = _adapter().parse("pull_request_review_comment", payload, org_id="org-1")
    assert ev is not None and ev.kind == "code_change_followup"
    assert ev.commit_sha == "feedbeef"


def test_parse_threaded_reply_to_non_aurora_comment_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human↔human review thread (no Aurora-authored parent) MUST NOT
    trigger a followup investigation. Fixes the thread-reply spoofing
    vector found by adversarial review."""
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="pull_request_review_comment",
        body="agreed, that's not right",
        in_reply_to_id=999,
    )
    # Parent comment is from another human reviewer, not Aurora.
    payload["comment"]["in_reply_to"] = {"user": {"login": "human-reviewer"}}
    # DB-lookup fallback ALSO finds no matching inline_comment_id; we
    # simulate that by patching the helper to return False directly.
    from services.change_intercept.adapters import github as adapter_module

    monkeypatch.setattr(
        adapter_module, "_parent_comment_is_aurora", lambda **_: False
    )
    assert (
        _adapter().parse("pull_request_review_comment", payload, org_id="org-1")
        is None
    )


def test_parse_comment_from_own_bot_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="issue_comment",
        body="this is an Aurora-authored comment",
        sender="aurora-test[bot]",
    )
    assert _adapter().parse("issue_comment", payload, org_id="org-1") is None


def test_parse_issue_comment_on_a_non_pr_issue_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(event_type="issue_comment", body="hey @aurora-test")
    payload["issue"].pop("pull_request")  # plain Issue, not a PR
    assert _adapter().parse("issue_comment", payload, org_id="org-1") is None


def test_parse_unrelated_comment_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(event_type="issue_comment", body="random chatter no mention")
    assert _adapter().parse("issue_comment", payload, org_id="org-1") is None


def test_parse_returns_none_for_unsupported_event_type() -> None:
    payload = {"action": "completed", "installation": {"id": 1}}
    assert _adapter().parse("workflow_run", payload, org_id="org-1") is None


# ─── is_reply_to_us classification ───────────────────────────────────


def test_is_reply_classifies_at_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="issue_comment", body="hey @aurora-test thoughts?"
    )
    m = _adapter().is_reply_to_us("issue_comment", payload)
    assert isinstance(m, ReplyMatch) and m.match_kind == "mention"


def test_is_reply_classifies_re_review_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="issue_comment", body="@aurora-test please re-review"
    )
    m = _adapter().is_reply_to_us("issue_comment", payload)
    assert m is not None and m.match_kind == "re_review"


def test_is_reply_classifies_threaded_reply_when_parent_is_aurora(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="pull_request_review_comment",
        body="that's not quite right",
        in_reply_to_id=999,
    )
    payload["comment"]["in_reply_to"] = {"user": {"login": "aurora-test[bot]"}}
    m = _adapter().is_reply_to_us("pull_request_review_comment", payload)
    assert m is not None and m.match_kind == "threaded"


def test_is_reply_filters_at_mention_from_own_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(
        event_type="issue_comment",
        body="@aurora-test some self-quote",
        sender="aurora-test[bot]",
    )
    assert _adapter().is_reply_to_us("issue_comment", payload) is None


def test_is_reply_at_mention_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
    payload = _comment_payload(event_type="issue_comment", body="@AURORA-TEST please look")
    assert _adapter().is_reply_to_us("issue_comment", payload) is not None


def test_is_reply_ignores_substring_username_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``@aurora-test`` must not match ``@aurora-testing-tool`` mid-word.
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora")
    payload = _comment_payload(
        event_type="issue_comment", body="cc @aurora-testing-tool not me"
    )
    assert _adapter().is_reply_to_us("issue_comment", payload) is None


def test_is_reply_returns_none_when_slug_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive: an unset slug must NOT match every comment.
    monkeypatch.delenv("NEXT_PUBLIC_GITHUB_APP_SLUG", raising=False)
    payload = _comment_payload(event_type="issue_comment", body="hey @anyone")
    assert _adapter().is_reply_to_us("issue_comment", payload) is None


# ─── verify_signature delegation ─────────────────────────────────────


def test_verify_signature_delegates_to_existing_util(
    monkeypatch: pytest.MonkeyPatch, webhook_secret: str
) -> None:
    body = b'{"action":"opened","number":42}'
    digest = hmac.new(webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={digest}"}

    monkeypatch.setattr(
        adapter_module, "get_app_webhook_secret", lambda: webhook_secret
    )
    assert _adapter().verify_signature(body, headers) is True


def test_verify_signature_returns_false_on_tampered_body(
    monkeypatch: pytest.MonkeyPatch, webhook_secret: str
) -> None:
    body = b'{"action":"opened","number":42}'
    tampered = b'{"action":"opened","number":99}'
    digest = hmac.new(webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={digest}"}

    monkeypatch.setattr(
        adapter_module, "get_app_webhook_secret", lambda: webhook_secret
    )
    assert _adapter().verify_signature(tampered, headers) is False


def test_verify_signature_returns_false_on_missing_header(
    monkeypatch: pytest.MonkeyPatch, webhook_secret: str
) -> None:
    monkeypatch.setattr(
        adapter_module, "get_app_webhook_secret", lambda: webhook_secret
    )
    assert _adapter().verify_signature(b"x", headers={}) is False


def test_verify_signature_returns_false_when_secret_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from connectors.github_connector.vault_keys import GitHubAppConfigError

    def boom() -> str:
        raise GitHubAppConfigError("not configured")

    monkeypatch.setattr(adapter_module, "get_app_webhook_secret", boom)
    headers = {"X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert _adapter().verify_signature(b"x", headers) is False


# ─── fetch_snapshot wiring ───────────────────────────────────────────


def test_fetch_snapshot_skips_when_install_or_repo_missing() -> None:
    event = NormalizedChangeEvent(
        vendor="github",
        kind="code_change",
        org_id="org-1",
        installation_id=None,  # no install
        external_id="42",
        dedup_key="github:acme/widgets:42",
        repo=None,
        ref=None,
        base_ref=None,
        commit_sha=None,
        actor=None,
        target_env=None,
        action="opened",
    )
    snap = _adapter().fetch_snapshot(event)
    assert isinstance(snap, ChangeSnapshot)
    assert snap.body == "" and snap.diff == ""


def test_fetch_snapshot_aggregates_rest_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub the REST helpers so the adapter never actually hits the
    # network. Each helper returns the shape the adapter unwraps.
    def fake_get(url: str, installation_id: int, *, accept: str = "", params: Any = None) -> Any:
        response = MagicMock()
        if accept == "application/vnd.github.diff":
            response.text = (
                "diff --git a/foo.py b/foo.py\n"
                "--- a/foo.py\n"
                "+++ b/foo.py\n"
                "@@ -1,1 +1,2 @@\n"
                " keep\n"
                "+added\n"
            )
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = {
                "body": "PR body text",
                "title": "doesn't matter",
            }
        return response

    fake_pages: dict[str, list[Any]] = {
        "files": [
            {
                "filename": "foo.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
            }
        ],
        "commits": [
            {
                "sha": "deadbeef",
                "commit": {"message": "tighten retry"},
                "author": {"login": "alice"},
            }
        ],
    }

    def fake_paginated(url: str, installation_id: int, **_kwargs: Any) -> list[Any]:
        if url.endswith("/files"):
            return fake_pages["files"]
        if url.endswith("/commits"):
            return fake_pages["commits"]
        # comments: not requested for a non-followup
        return []

    monkeypatch.setattr(adapter_module, "_get", fake_get)
    monkeypatch.setattr(adapter_module, "_paginated_get", fake_paginated)

    event = NormalizedChangeEvent(
        vendor="github",
        kind="code_change",
        org_id="org-1",
        installation_id=99999,
        external_id="42",
        dedup_key="github:acme/widgets:42",
        repo="acme/widgets",
        ref="feat/foo",
        base_ref="main",
        commit_sha="deadbeef",
        actor="alice",
        target_env="prod",
        action="opened",
    )
    snap = _adapter().fetch_snapshot(event)
    assert snap.body == "PR body text"
    assert "+added" in snap.diff
    assert snap.files == [
        {"path": "foo.py", "status": "modified", "additions": 1, "deletions": 0}
    ]
    assert snap.commits[0]["message"] == "tighten retry"
    assert snap.commits[0]["author"] == "alice"
    # Non-followup events skip the comments round-trip.
    assert snap.comments == []


def test_fetch_snapshot_fetches_comments_for_followups(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*_a: Any, **_kw: Any) -> Any:
        r = MagicMock()
        r.text = ""
        r.json.return_value = {"body": ""}
        return r

    def fake_paginated(url: str, *_a: Any, **_kw: Any) -> list[Any]:
        if url.endswith("/issues/42/comments"):
            return [
                {
                    "id": 1,
                    "user": {"login": "alice"},
                    "body": "first comment",
                    "in_reply_to_id": None,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        if url.endswith("/pulls/42/comments"):
            return [
                {
                    "id": 2,
                    "user": {"login": "aurora-test[bot]"},
                    "body": "inline note from Aurora",
                    "in_reply_to_id": None,
                    "created_at": "2026-01-01T01:00:00Z",
                }
            ]
        return []

    monkeypatch.setattr(adapter_module, "_get", fake_get)
    monkeypatch.setattr(adapter_module, "_paginated_get", fake_paginated)

    event = NormalizedChangeEvent(
        vendor="github",
        kind="code_change_followup",
        org_id="org-1",
        installation_id=99999,
        external_id="comment:5",
        dedup_key="github:acme/widgets:42",
        repo="acme/widgets",
        ref=None,
        base_ref=None,
        commit_sha=None,
        actor="bob",
        target_env=None,
        action="reply",
        parent_external_id="42",
        follow_up_comment="re-review please",
    )
    snap = _adapter().fetch_snapshot(event)
    kinds = {c["kind"] for c in snap.comments}
    assert kinds == {"issue", "review"}
    assert any(c["user_login"] == "aurora-test[bot]" for c in snap.comments)

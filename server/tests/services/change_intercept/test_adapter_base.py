"""Unit tests for the vendor-neutral adapter dataclasses + registry.

The dataclasses are the contract every adapter implements; the
registry is the single point the dispatcher uses to look them up.
A regression in either silently breaks every vendor that lands later,
so we pin the surface aggressively.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from services.change_intercept.adapters import base, registry
from services.change_intercept.adapters.base import (
    RE_REVIEW_COMMANDS,
    ChangeSnapshot,
    Finding,
    NormalizedChangeEvent,
    PostedVerdict,
    ReplyMatch,
)
from services.change_intercept.adapters.registry import (
    UnknownVendorError,
    get_adapter,
    registered_vendors,
)


# ─── Dataclass contracts ─────────────────────────────────────────────


def test_normalized_change_event_is_frozen_and_serialisable() -> None:
    ev = NormalizedChangeEvent(
        vendor="github",
        kind="code_change",
        org_id="org-1",
        installation_id=42,
        external_id="100",
        dedup_key="github:acme/widgets:100",
        repo="acme/widgets",
        ref="feat/foo",
        base_ref="main",
        commit_sha="deadbeef",
        actor="alice",
        target_env="prod",
        action="opened",
    )
    # ``asdict`` is what the dispatcher uses to JSON-serialise the
    # event for the audit log + database row.
    payload = dataclasses.asdict(ev)
    assert payload["dedup_key"] == "github:acme/widgets:100"
    # Mutation must fail.
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.kind = "rewritten"  # type: ignore[misc]


def test_change_snapshot_defaults_are_independent() -> None:
    # Default list/dict fields must NOT share state across instances —
    # this is the classic ``dataclass(field(default=[]))`` gotcha. The
    # production code uses ``field(default_factory=list)``; pin it here.
    a = ChangeSnapshot(body="a", diff="")
    b = ChangeSnapshot(body="b", diff="")
    assert a.files is not b.files
    assert a.commits is not b.commits
    assert a.comments is not b.comments


def test_finding_optional_end_line_defaults_to_none() -> None:
    finding = Finding(
        severity="HIGH",
        confidence="HIGH",
        category="missing_timeout",
        file_path="server/foo.py",
        start_line=42,
        title="HTTP client without timeout",
        rationale="requests.get on line 42 has no timeout kwarg",
    )
    assert finding.end_line is None
    assert finding.cited_tool_calls == []


def test_posted_verdict_default_inline_ids_is_empty_list() -> None:
    v = PostedVerdict(verdict_id="rev-123")
    assert v.inline_comment_ids == []


def test_reply_match_carries_all_required_fields() -> None:
    m = ReplyMatch(
        repo="acme/widgets",
        parent_pr_external_id="100",
        comment_id="555",
        comment_body="hey @aurora rethink this",
        replier="bob",
        match_kind="mention",
    )
    assert m.parent_pr_external_id == "100"
    assert m.match_kind == "mention"


def test_re_review_commands_are_lowercase_for_case_insensitive_match() -> None:
    assert all(cmd == cmd.lower() for cmd in RE_REVIEW_COMMANDS)
    # The taxonomy of recheck triggers is intentionally tight — broader
    # phrases invite false positives from casual @-mentions.
    assert len(RE_REVIEW_COMMANDS) <= 6


# ─── Registry contracts ──────────────────────────────────────────────


def test_registered_vendors_contains_github() -> None:
    assert "github" in registered_vendors()


def test_get_adapter_returns_cached_instance() -> None:
    registry._reset_for_tests()
    a1 = get_adapter("github")
    a2 = get_adapter("github")
    assert a1 is a2


def test_get_adapter_raises_for_unknown_vendor() -> None:
    with pytest.raises(UnknownVendorError):
        get_adapter("not-a-real-vendor")


def test_unknown_vendor_error_is_a_keyerror_subclass() -> None:
    # Lets callers ``except KeyError`` without a separate import — useful
    # for dispatcher fall-back paths.
    assert issubclass(UnknownVendorError, KeyError)


def test_adapter_protocol_methods_exist() -> None:
    """Every adapter MUST expose all six methods + ``vendor`` attribute."""
    adapter: Any = get_adapter("github")
    assert hasattr(adapter, "vendor")
    assert isinstance(adapter.vendor, str) and adapter.vendor
    for method_name in (
        "verify_signature",
        "parse",
        "fetch_snapshot",
        "is_reply_to_us",
        "post_verdict",
        "dismiss_prior",
    ):
        assert callable(getattr(adapter, method_name)), (
            f"adapter missing required method: {method_name}"
        )


def test_post_verdict_and_dismiss_prior_are_callable_in_part_3() -> None:
    """Phase 1a Part 3 wires the real implementation. The customer-
    visible-review safety net is no longer at the method-implementation
    level; it now lives in the ``github_installations.change_intercept_dry_run``
    flag (default TRUE) which the Celery task consults before calling
    post_verdict.

    This test only asserts the methods are real (no longer
    NotImplementedError). End-to-end behaviour is covered by
    ``test_github_adapter`` which patches the HTTP layer."""
    adapter: Any = get_adapter("github")
    # Methods must exist and not raise NotImplementedError at lookup.
    # Real network calls are exercised in test_github_adapter with
    # mocked HTTP; here we just pin the contract that the symbols
    # remain callable post-Part-3.
    assert callable(adapter.post_verdict)
    assert callable(adapter.dismiss_prior)


def test_base_module_re_exports_required_symbols() -> None:
    # Spot-check that the dispatcher's lazy import path will resolve.
    assert hasattr(base, "ChangeAdapter")
    assert hasattr(base, "NormalizedChangeEvent")
    assert hasattr(base, "ChangeSnapshot")
    assert hasattr(base, "Finding")
    assert hasattr(base, "PostedVerdict")
    assert hasattr(base, "ReplyMatch")
    assert hasattr(base, "RE_REVIEW_COMMANDS")

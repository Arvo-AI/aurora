"""Vendor-neutral contracts for the change-intercept pipeline.

Every vendor adapter (GitHub, GitLab, Bitbucket, ...) converts its
webhook payloads into these dataclasses. The core pipeline (Celery
tasks, investigator, validator) only speaks this language.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, runtime_checkable


@dataclasses.dataclass(frozen=True, slots=True)
class NormalizedChangeEvent:
    """Identity + metadata of a change, parsed from a webhook payload."""

    org_id: str
    vendor: str
    kind: str  # 'code_change' | 'code_change_followup'
    external_id: str
    dedup_key: str
    repo: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    actor: str | None = None
    target_env: str | None = None
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class ChangeSnapshot:
    """Content fetched once at webhook time — the investigator's input."""

    change_body: str | None = None
    change_diff: str | None = None
    change_files: list[dict[str, Any]] | None = None
    change_commits: list[dict[str, Any]] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ReplyMatch:
    """A comment that is addressed to Aurora on a prior review."""

    original_event_dedup_key: str
    reply_body: str
    parent_investigation_id: str | None = None


@runtime_checkable
class ChangeAdapter(Protocol):
    """Six-method contract every vendor adapter must satisfy.

    The core pipeline calls *only* these methods.  Everything
    vendor-specific — signature format, payload shape, API auth,
    review idiom — lives behind them.
    """

    vendor: str

    def verify_signature(self, request: Any) -> bool:
        """Validate the webhook signature on the raw request."""
        ...

    def parse(self, request: Any) -> NormalizedChangeEvent | None:
        """Convert a webhook payload to a NormalizedChangeEvent.

        Return None to silently ignore the event (e.g. unsupported action).
        """
        ...

    def fetch_snapshot(self, event: NormalizedChangeEvent) -> ChangeSnapshot:
        """One-shot fetch of diff, files, commits, and body."""
        ...

    def is_reply_to_us(self, request: Any) -> ReplyMatch | None:
        """Classify a comment event as a reply to Aurora, or None."""
        ...

    def post_verdict(self, event: Any, investigation: Any) -> str:
        """Post the investigation verdict; return the vendor-native id."""
        ...

    def dismiss_prior(self, event: Any, prior_verdict_id: str) -> None:
        """Dismiss/remove a previous Aurora verdict before re-posting."""
        ...

"""GitHub adapter for the change-intercept pipeline.

Implements the ChangeAdapter protocol for GitHub pull-request webhooks.
Methods that require a live GitHub App (fetch_snapshot, post_verdict,
dismiss_prior) raise NotImplementedError until Phase 0 (App migration)
provides the auth layer.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from services.change_intercept.adapters.base import (
    ChangeAdapter,
    ChangeSnapshot,
    NormalizedChangeEvent,
    ReplyMatch,
)
from utils.web.webhook_signature import verify_github_signature

logger = logging.getLogger(__name__)

_SUPPORTED_PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

_TARGET_ENV_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(main|master|prod.*)$", re.IGNORECASE), "production"),
    (re.compile(r"^(stag|staging.*)$", re.IGNORECASE), "staging"),
    (re.compile(r"^(dev|develop.*)$", re.IGNORECASE), "development"),
]


def _infer_target_env(base_ref: str | None) -> str | None:
    if not base_ref:
        return None
    for pattern, env in _TARGET_ENV_PATTERNS:
        if pattern.match(base_ref):
            return env
    return None


class GitHubAdapter:
    """ChangeAdapter implementation for GitHub pull requests."""

    vendor: str = "github"

    def __init__(self) -> None:
        self._webhook_secret = os.getenv("GITHUB_APP_WEBHOOK_SECRET", "")
        self._app_slug = os.getenv("GITHUB_APP_SLUG", "aurora-app")

    # ------------------------------------------------------------------
    # Inbound (webhook → normalized event)
    # ------------------------------------------------------------------

    def verify_signature(self, request: Any) -> bool:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not sig:
            logger.warning("[ChangeIntercept:GitHub] Missing X-Hub-Signature-256")
            return False
        return verify_github_signature(request.get_data(), sig, self._webhook_secret)

    def parse(self, request: Any) -> NormalizedChangeEvent | None:
        event_type = request.headers.get("X-GitHub-Event", "")
        payload: dict[str, Any] = request.get_json(silent=True) or {}

        if event_type != "pull_request":
            return None

        action = payload.get("action", "")
        if action not in _SUPPORTED_PR_ACTIONS:
            return None

        pr = payload.get("pull_request", {})
        repo_full = payload.get("repository", {}).get("full_name", "")
        pr_number = pr.get("number")
        head = pr.get("head", {})
        base = pr.get("base", {})
        installation_id = payload.get("installation", {}).get("id")

        if not repo_full or pr_number is None or not installation_id:
            logger.warning("[ChangeIntercept:GitHub] Incomplete PR payload")
            return None

        return NormalizedChangeEvent(
            org_id=self._resolve_org_id(installation_id),
            vendor=self.vendor,
            kind="code_change",
            external_id=f"{repo_full}#{pr_number}",
            dedup_key=f"{repo_full}#{pr_number}",
            repo=repo_full,
            ref=base.get("ref"),
            commit_sha=head.get("sha"),
            actor=pr.get("user", {}).get("login"),
            target_env=_infer_target_env(base.get("ref")),
            payload=payload,
        )

    def is_reply_to_us(self, request: Any) -> ReplyMatch | None:
        event_type = request.headers.get("X-GitHub-Event", "")
        payload: dict[str, Any] = request.get_json(silent=True) or {}

        sender = payload.get("sender", {})
        if sender.get("login", "").endswith("[bot]") and sender.get("type") == "Bot":
            return None  # self-filter: ignore our own comments

        bot_login = f"{self._app_slug}[bot]"

        if event_type == "pull_request_review_comment":
            comment = payload.get("comment", {})
            in_reply_to = comment.get("in_reply_to_id")
            if not in_reply_to:
                return None
            # We can't verify the parent author without an API call here,
            # so we check if the comment body references Aurora or is in
            # a thread. The webhook handler will cross-check the DB.
            return self._build_reply_match(payload, comment.get("body", ""))

        if event_type == "issue_comment":
            comment = payload.get("comment", {})
            body = comment.get("body", "")
            if f"@{bot_login}" in body or f"@{self._app_slug}" in body:
                return self._build_reply_match(payload, body)

        return None

    # ------------------------------------------------------------------
    # Outbound (investigation → vendor) — stubbed until Phase 0
    # ------------------------------------------------------------------

    def fetch_snapshot(self, event: NormalizedChangeEvent) -> ChangeSnapshot:
        raise NotImplementedError(
            "GitHubAdapter.fetch_snapshot requires App auth (Phase 0)"
        )

    def post_verdict(self, event: Any, investigation: Any) -> str:
        raise NotImplementedError(
            "GitHubAdapter.post_verdict requires App auth (Phase 0)"
        )

    def dismiss_prior(self, event: Any, prior_verdict_id: str) -> None:
        raise NotImplementedError(
            "GitHubAdapter.dismiss_prior requires App auth (Phase 0)"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_org_id(installation_id: int) -> str:
        """Map a GitHub App installation_id to an Aurora org_id.

        Looks up the github_app_installations table.  Falls back to the
        installation_id as a string if not found (the event will still
        be persisted and can be linked later).
        """
        try:
            from utils.db.connection_pool import db_pool

            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM github_app_installations "
                        "WHERE installation_id = %s AND suspended_at IS NULL",
                        (installation_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        return row[0]
        except Exception as exc:
            logger.warning(
                "[ChangeIntercept:GitHub] org_id lookup failed for "
                "installation %s: %s",
                installation_id,
                exc,
            )
        return str(installation_id)

    def _build_reply_match(
        self, payload: dict[str, Any], body: str
    ) -> ReplyMatch | None:
        pr = payload.get("pull_request") or payload.get("issue", {})
        repo_full = payload.get("repository", {}).get("full_name", "")
        pr_number = pr.get("number") if pr else None
        if not repo_full or pr_number is None:
            return None
        return ReplyMatch(
            original_event_dedup_key=f"{repo_full}#{pr_number}",
            reply_body=body,
        )

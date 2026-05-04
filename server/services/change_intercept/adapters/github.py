"""GitHub adapter for the change-intercept pipeline.

Implements the ChangeAdapter protocol for GitHub pull-request webhooks.
Outbound methods (fetch_snapshot, post_verdict, dismiss_prior) use
the GitHub App installation token from utils.auth.github_app_token.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

from services.change_intercept.adapters.base import (
    ChangeSnapshot,
    NormalizedChangeEvent,
    ReplyMatch,
)

logger = logging.getLogger(__name__)

_SUPPORTED_PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

_TARGET_ENV_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(main|master|prod.*)$", re.IGNORECASE), "production"),
    (re.compile(r"^(stag|staging.*)$", re.IGNORECASE), "staging"),
    (re.compile(r"^(dev|develop.*)$", re.IGNORECASE), "development"),
]

_GH_API = "https://api.github.com"
_GH_ACCEPT = "application/vnd.github+json"
_GH_API_VERSION = "2022-11-28"
_GH_TIMEOUT = 20
_GH_DIFF_ACCEPT = "application/vnd.github.v3.diff"
_MAX_DIFF_BYTES = 512_000


def _infer_target_env(base_ref: str | None) -> str | None:
    if not base_ref:
        return None
    for pattern, env in _TARGET_ENV_PATTERNS:
        if pattern.match(base_ref):
            return env
    return None


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": _GH_ACCEPT,
        "X-GitHub-Api-Version": _GH_API_VERSION,
    }


class GitHubAdapter:
    """ChangeAdapter implementation for GitHub pull requests."""

    vendor: str = "github"

    def __init__(self) -> None:
        self._app_slug = os.getenv("GITHUB_APP_SLUG", "aurora-app")

    # ------------------------------------------------------------------
    # Inbound (webhook -> normalized event)
    # ------------------------------------------------------------------

    def verify_signature(self, request: Any) -> bool:
        # Signature verification is handled by Haled's webhook route
        # (server/routes/github/github_webhook.py). This method exists
        # only to satisfy the ChangeAdapter protocol.
        return True

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
            return None

        bot_login = f"{self._app_slug}[bot]"

        if event_type == "pull_request_review_comment":
            comment = payload.get("comment", {})
            if not comment.get("in_reply_to_id"):
                return None
            return self._build_reply_match(payload, comment.get("body", ""))

        if event_type == "issue_comment":
            comment = payload.get("comment", {})
            body = comment.get("body", "")
            if f"@{bot_login}" in body or f"@{self._app_slug}" in body:
                return self._build_reply_match(payload, body)

        return None

    # ------------------------------------------------------------------
    # Outbound (investigation -> vendor)
    # ------------------------------------------------------------------

    def fetch_snapshot(
        self,
        event: NormalizedChangeEvent,
        *,
        installation_id: int | None = None,
    ) -> ChangeSnapshot:
        """Fetch the PR diff, file list, commits, and body from GitHub."""
        from utils.auth.github_app_token import get_installation_token

        inst_id = installation_id or self._installation_id_from_event(event)
        if not inst_id:
            raise ValueError("No installation_id available for fetch_snapshot")

        token = get_installation_token(inst_id)
        headers = _gh_headers(token)
        repo, pr_number = self._parse_dedup_key(event.dedup_key)

        pr_url = f"{_GH_API}/repos/{repo}/pulls/{pr_number}"
        pr_resp = requests.get(pr_url, headers=headers, timeout=_GH_TIMEOUT)
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()

        diff_headers = {**headers, "Accept": _GH_DIFF_ACCEPT}
        diff_resp = requests.get(pr_url, headers=diff_headers, timeout=_GH_TIMEOUT)
        diff_resp.raise_for_status()
        diff_text = diff_resp.text[:_MAX_DIFF_BYTES]

        files_resp = requests.get(
            f"{pr_url}/files", headers=headers, timeout=_GH_TIMEOUT,
            params={"per_page": 100},
        )
        files_resp.raise_for_status()
        files_data = files_resp.json()

        commits_resp = requests.get(
            f"{pr_url}/commits", headers=headers, timeout=_GH_TIMEOUT,
            params={"per_page": 100},
        )
        commits_resp.raise_for_status()
        commits_data = commits_resp.json()

        return ChangeSnapshot(
            change_body=pr_data.get("body") or "",
            change_diff=diff_text,
            change_files=[
                {"filename": f.get("filename"), "status": f.get("status"),
                 "additions": f.get("additions"), "deletions": f.get("deletions")}
                for f in files_data
            ],
            change_commits=[
                {"sha": c.get("sha"),
                 "message": (c.get("commit") or {}).get("message", "")}
                for c in commits_data
            ],
        )

    def post_verdict(self, event: Any, investigation: Any) -> str:
        """Post a PR review with the investigation verdict."""
        from utils.auth.github_app_token import get_installation_token

        inst_id = event.get("installation_id")
        if not inst_id:
            raise ValueError("No installation_id on change_event for post_verdict")

        token = get_installation_token(inst_id)
        headers = _gh_headers(token)
        repo, pr_number = self._parse_dedup_key(event["dedup_key"])

        verdict = investigation["verdict"]
        rationale = investigation["rationale"]
        gh_event = "APPROVE" if verdict == "approve" else "REQUEST_CHANGES"

        body = f"**Aurora Change Risk Assessment: {verdict.upper()}**\n\n{rationale}"

        cited = investigation.get("cited_findings")
        if cited:
            try:
                findings = json.loads(cited) if isinstance(cited, str) else cited
                if findings:
                    body += "\n\n**Cited Findings:**\n"
                    for f in findings:
                        body += f"\n- {f.get('summary', f)}"
            except (json.JSONDecodeError, TypeError):
                pass

        intent = investigation.get("intent_alignment")
        if intent:
            body += f"\n\n**Intent Alignment:** {intent}"
            notes = investigation.get("intent_notes")
            if notes:
                body += f" - {notes}"

        review_url = f"{_GH_API}/repos/{repo}/pulls/{pr_number}/reviews"
        resp = requests.post(
            review_url,
            headers=headers,
            json={"body": body, "event": gh_event},
            timeout=_GH_TIMEOUT,
        )
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    def dismiss_prior(self, event: Any, prior_verdict_id: str) -> None:
        """Dismiss a previous Aurora review on the PR."""
        from utils.auth.github_app_token import get_installation_token

        inst_id = event.get("installation_id")
        if not inst_id:
            return

        token = get_installation_token(inst_id)
        headers = _gh_headers(token)
        repo, pr_number = self._parse_dedup_key(event["dedup_key"])

        dismiss_url = (
            f"{_GH_API}/repos/{repo}/pulls/{pr_number}"
            f"/reviews/{prior_verdict_id}/dismissals"
        )
        resp = requests.put(
            dismiss_url,
            headers=headers,
            json={"message": "Superseded by updated Aurora review."},
            timeout=_GH_TIMEOUT,
        )
        if resp.status_code == 404:
            logger.debug("[ChangeIntercept:GitHub] Review %s already gone", prior_verdict_id)
            return
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_org_id(installation_id: int) -> str:
        """Map a GitHub App installation_id to an Aurora org_id.

        Uses the user_github_installations table (Haled's migration).
        Falls back to the installation_id as a string if not found.
        """
        try:
            from utils.db.connection_pool import db_pool

            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM user_github_installations "
                        "WHERE installation_id = %s LIMIT 1",
                        (installation_id,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0]
        except Exception as exc:
            logger.warning(
                "[ChangeIntercept:GitHub] org_id lookup failed for "
                "installation %s: %s",
                installation_id, exc,
            )
        return str(installation_id)

    def _build_reply_match(
        self, payload: dict[str, Any], body: str,
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

    @staticmethod
    def _parse_dedup_key(dedup_key: str) -> tuple[str, int]:
        """Parse ``owner/repo#123`` into ``('owner/repo', 123)``."""
        repo, _, num = dedup_key.rpartition("#")
        return repo, int(num)

    @staticmethod
    def _installation_id_from_event(event: NormalizedChangeEvent) -> int | None:
        return event.payload.get("installation", {}).get("id")

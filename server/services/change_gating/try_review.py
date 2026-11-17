"""Zero-connection "try a review": pre-merge risk review with nothing connected.

Today an evaluator has to install the GitHub App (and get it past a security
review) before Aurora shows them anything. This module lets a signed-in user
paste either

* the URL of a **public** GitHub pull request, or
* a unified diff (``git diff main...HEAD``) — works for private code too,

and get the same Aurora risk review the GitHub App posts, with no connector,
no credentials and nothing written back to GitHub.

Design notes
------------
- Everything here is pure (no Flask / Celery / DB imports) so it is unit-testable
  without the stack. The Celery task (``tasks/change_gating_try.py``) wires in
  the real agent run and Redis.
- It reuses the production pipeline: ``build_review_prompt`` ->
  agent (read-only ``ask`` mode) -> ``parse_verdict`` -> ``anchor_findings``.
  The preview only differs in (a) how the PR is fetched and (b) a preview note
  that tells the agent it has the diff and nothing else, because the production
  prompt promises live monitoring / deploy / Slack context that does not exist
  here and would invite invented "evidence".
- SSRF: the only user input that reaches a network call is ``owner/repo/number``
  extracted by a strict regex; the API URL is built from those parts against a
  hardcoded host, and redirects are never followed.
- Nothing is posted anywhere. Aurora never approves; there is no write path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from services.change_gating.diff_utils import anchor_findings, parse_diff_hunks
from services.change_gating.verdict import (
    build_review_prompt,
    extract_verdict_with_llm,
    parse_verdict,
    render_review_body,
)

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# Pasted diffs are bounded at the route; this is the hard ceiling the pure
# layer enforces regardless of caller.
MAX_DIFF_BYTES = 300_000
_TIMEOUT_SECONDS = 20
# Unauthenticated GitHub allows 60 requests/hour/IP; a review needs ~2 + pages.
# 3 pages x 100 files covers nearly every real PR without burning the budget.
_MAX_FILE_PAGES = 3
_PER_PAGE = 100

# GitHub login / repo name charset. No slashes, no dots-only names.
_SEGMENT = r"[A-Za-z0-9_.-]{1,100}"
_PR_URL_RE = re.compile(
    rf"^https://(?:www\.)?github\.com/(?P<owner>{_SEGMENT})/(?P<repo>{_SEGMENT})"
    r"/pull/(?P<number>[0-9]{1,9})(?:/[A-Za-z0-9_./-]*)?(?:[?#].*)?$"
)


class TryReviewError(Exception):
    """A user-presentable failure. ``code`` is stable for the UI to branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PublicPRRef:
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_public_pr_url(url: str) -> PublicPRRef:
    """Parse ``https://github.com/<owner>/<repo>/pull/<n>`` strictly.

    Anything else — other hosts, ``http://``, userinfo tricks
    (``github.com@evil``), path traversal, enterprise hosts — is rejected, so
    the string that reaches the network is always built from validated parts.
    """
    candidate = (url or "").strip()
    match = _PR_URL_RE.match(candidate)
    if not match:
        raise TryReviewError(
            "invalid_url",
            "Paste a pull request URL like https://github.com/owner/repo/pull/123, "
            "or paste the diff itself.",
        )
    owner, repo = match.group("owner"), match.group("repo")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise TryReviewError("invalid_url", "That does not look like a GitHub pull request URL.")
    return PublicPRRef(owner=owner, repo=repo, number=int(match.group("number")))


# ---------------------------------------------------------------------------
# Public (unauthenticated) GitHub reader
# ---------------------------------------------------------------------------


class PublicGitHubReader:
    """Read-only, unauthenticated GitHub client for one public PR.

    Sends no ``Authorization`` header, so it can only ever see what any
    anonymous visitor sees. Records every request path it made in
    ``requests_made`` so the UI can show exactly what was read.
    """

    def __init__(self, ref: PublicPRRef, session: Optional[requests.Session] = None):
        self.ref = ref
        self._session = session or requests.Session()
        self.requests_made: List[str] = []

    def close(self) -> None:
        self._session.close()

    def _get(self, path: str, accept: str, params: Optional[Dict[str, Any]] = None):
        url = f"{GITHUB_API_BASE}/repos/{self.ref.full_name}{path}"
        self.requests_made.append(f"GET /repos/{self.ref.full_name}{path}")
        try:
            response = self._session.get(
                url,
                headers={
                    "Accept": accept,
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "aurora-try-review",
                },
                params=params,
                timeout=_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise TryReviewError(
                "github_unreachable",
                "Could not reach GitHub from this server. Paste the diff instead.",
            ) from exc
        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response) -> None:
        status = response.status_code
        if status < 300:
            return
        if status in (301, 302, 307, 308):
            raise TryReviewError(
                "moved",
                "That repository was renamed or moved. Paste the PR's current URL.",
            )
        if status == 404:
            raise TryReviewError(
                "not_found_or_private",
                "Could not read that pull request without credentials. It may be private "
                "or the URL may be wrong. Paste its diff instead "
                "(git diff main...HEAD) — no connection needed.",
            )
        remaining = (getattr(response, "headers", None) or {}).get("X-RateLimit-Remaining")
        body = (getattr(response, "text", None) or "").lower()
        if status == 429 or (status == 403 and (remaining == "0" or "rate limit" in body)):
            raise TryReviewError(
                "rate_limited",
                "GitHub's anonymous rate limit was hit for this server. Try again in a few "
                "minutes, or paste the diff instead.",
            )
        raise TryReviewError("github_error", f"GitHub returned an unexpected status ({status}).")

    def get_pull_request(self) -> Dict[str, Any]:
        data = self._get(f"/pulls/{self.ref.number}", "application/vnd.github+json").json()
        if not isinstance(data, dict):
            raise TryReviewError("github_error", "GitHub returned an unexpected response.")
        return data

    def get_diff(self) -> Optional[str]:
        """Whole-PR unified diff, or None when GitHub refuses (406: too large)."""
        try:
            return self._get(
                f"/pulls/{self.ref.number}", "application/vnd.github.v3.diff"
            ).text
        except TryReviewError as exc:
            # 406 (diff too large to serve) surfaces as the generic github_error;
            # the per-file patches already cover that case, so degrade to None.
            # Every other failure (404, rate limit, moved) is real and re-raised.
            if exc.code == "github_error":
                return None
            raise

    def list_files(self) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        for page in range(1, _MAX_FILE_PAGES + 1):
            batch = self._get(
                f"/pulls/{self.ref.number}/files",
                "application/vnd.github+json",
                params={"per_page": _PER_PAGE, "page": page},
            ).json()
            if not isinstance(batch, list) or not batch:
                break
            files.extend(f for f in batch if isinstance(f, dict))
            if len(batch) < _PER_PAGE:
                break
        return files


# ---------------------------------------------------------------------------
# Pasted-diff parsing
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _count_changes(patch: str) -> tuple[int, int]:
    """Hunk-aware (+/-) counts so content like ``-- comment`` is not a header."""
    additions = deletions = 0
    left = right = 0
    for line in patch.splitlines():
        if left > 0 or right > 0:
            if line.startswith("\\"):
                continue
            if line.startswith("-"):
                deletions += 1
                left -= 1
            elif line.startswith("+"):
                additions += 1
                right -= 1
            else:
                left -= 1
                right -= 1
            continue
        match = _HUNK_RE.match(line)
        if match:
            left = int(match.group(1)) if match.group(1) is not None else 1
            right = int(match.group(2)) if match.group(2) is not None else 1
    return additions, deletions


def split_unified_diff(diff_text: str) -> List[Dict[str, Any]]:
    """Split a ``git diff`` into GitHub ``list_files``-shaped dicts.

    Lets the production prompt builder treat pasted text exactly like a PR.
    """
    files: List[Dict[str, Any]] = []
    chunks = re.split(r"(?m)^(?=diff --git )", diff_text)
    for chunk in chunks:
        if not chunk.startswith("diff --git "):
            continue
        header_end = chunk.find("\n@@")
        header = chunk if header_end == -1 else chunk[:header_end]
        patch = "" if header_end == -1 else chunk[header_end + 1:]

        filename = None
        status = "modified"
        for line in header.splitlines():
            if line.startswith("+++ "):
                target = line[4:].split("\t")[0].strip()
                if target != "/dev/null":
                    filename = target[2:] if target.startswith("b/") else target
            elif line.startswith("--- ") and line[4:].strip() == "/dev/null":
                status = "added"
            elif line.startswith("new file mode"):
                status = "added"
            elif line.startswith("deleted file mode"):
                status = "removed"
            elif line.startswith("rename from"):
                status = "renamed"
        if filename is None:
            match = re.match(r"diff --git a/(.+?) b/(.+)$", header.splitlines()[0])
            filename = match.group(2) if match else "<unknown>"
        additions, deletions = _count_changes(patch)
        entry: Dict[str, Any] = {
            "filename": filename,
            "status": status,
            "additions": additions,
            "deletions": deletions,
        }
        if patch:
            entry["patch"] = patch
        files.append(entry)
    return files


def validate_pasted_diff(diff_text: str) -> str:
    text = diff_text or ""
    if len(text.encode("utf-8", "ignore")) > MAX_DIFF_BYTES:
        raise TryReviewError(
            "diff_too_large",
            f"That diff is larger than {MAX_DIFF_BYTES // 1000} KB. Paste a smaller slice "
            "(for example the infrastructure and pipeline files only).",
        )
    if "diff --git " not in text and "\n@@ " not in text and not text.startswith("@@ "):
        raise TryReviewError(
            "not_a_diff",
            "That does not look like a unified diff. Run `git diff main...HEAD` and paste the output.",
        )
    return text


# ---------------------------------------------------------------------------
# Preview prompt + orchestration
# ---------------------------------------------------------------------------

PREVIEW_NOTE = """PREVIEW MODE — READ FIRST (overrides the capability list below):
This is a no-connection preview. You have ONLY the pull request content shown
below. No monitoring, deployment history, CI status, Slack or live infrastructure
is connected for this review, and you must not claim or imply that you checked any.
Never invent alerts, deploys, incidents or error rates. Base every finding strictly
on what the diff itself shows, and still apply the VERDICT DECISION TEST. Where
live context would change your confidence, say so in the summary in one sentence
instead of guessing."""


def build_preview_prompt(
    repo_label: str,
    pr: Dict[str, Any],
    files: List[Dict[str, Any]],
    diff: Optional[str],
) -> str:
    return PREVIEW_NOTE + "\n\n" + build_review_prompt(repo_label, pr, files, diff)


def pasted_diff_pr(title: str = "Pasted diff") -> Dict[str, Any]:
    return {
        "number": None,
        "title": title,
        "body": "",
        "user": {"login": "unknown"},
        "base": {"ref": "base"},
        "head": {"ref": "pasted-diff", "sha": "pasted"},
    }


@dataclass
class PreviewContext:
    """Everything fetched for one preview, plus what to tell the user was read."""

    repo_label: str
    pr: Dict[str, Any]
    files: List[Dict[str, Any]]
    diff: Optional[str]
    source: Dict[str, Any]
    transparency: Dict[str, Any]


_STORED = [
    "The review result, kept for 1 hour in this Aurora instance's Redis so you can reload the page.",
    "A chat session in this Aurora instance (visible in your chat history) containing the prompt and the verdict.",
]
_NOT_STORED = [
    "No credentials (nothing was asked for and no Authorization header was sent to GitHub).",
    "Nothing is written to GitHub: no comment, no review, no approval.",
]


def load_public_pr(url: str, reader: Optional[PublicGitHubReader] = None) -> PreviewContext:
    ref = parse_public_pr_url(url)
    reader = reader or PublicGitHubReader(ref)
    try:
        pr = reader.get_pull_request()
        files = reader.list_files()
        diff = reader.get_diff() if not any(f.get("patch") for f in files) else None
    finally:
        reader.close()
    head = pr.get("head") or {}
    return PreviewContext(
        repo_label=ref.full_name,
        pr=pr,
        files=files,
        diff=diff,
        source={
            "kind": "public_pr",
            "repo": ref.full_name,
            "pr_number": ref.number,
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged": bool(pr.get("merged")),
            "head_sha": head.get("sha"),
            "url": f"https://github.com/{ref.full_name}/pull/{ref.number}",
        },
        transparency={
            "read": reader.requests_made,
            "stored": _STORED,
            "not_stored": _NOT_STORED,
        },
    )


def load_pasted_diff(diff_text: str) -> PreviewContext:
    text = validate_pasted_diff(diff_text)
    files = split_unified_diff(text)
    return PreviewContext(
        repo_label="pasted diff",
        pr=pasted_diff_pr(),
        files=files,
        diff=text,
        source={"kind": "diff", "files": len(files)},
        transparency={
            "read": ["Only the text you pasted. Nothing was fetched from any other system."],
            "stored": _STORED,
            "not_stored": _NOT_STORED[:1] + ["Nothing leaves this Aurora instance except the model call."],
        },
    )


def run_preview(
    ctx: PreviewContext,
    run_agent: Callable[[str, str], Optional[str]],
) -> Dict[str, Any]:
    """Run one preview review.

    ``run_agent(prompt, rail_text)`` executes the read-only agent and returns
    its final message (or None). It is injected so this stays testable and the
    Celery layer owns session creation.

    Returns a JSON-serialisable result. Raises :class:`TryReviewError` for
    outcomes the UI should explain (agent failed, verdict unreadable).
    """
    prompt = build_preview_prompt(ctx.repo_label, ctx.pr, ctx.files, ctx.diff)
    rail_text = (ctx.pr.get("title") or "") + "\n\n" + (ctx.pr.get("body") or "")
    final_text = run_agent(prompt, rail_text)
    if final_text is None:
        raise TryReviewError("agent_failed", "The review agent did not finish. Check the LLM configuration and try again.")

    verdict = parse_verdict(final_text) or extract_verdict_with_llm(final_text)
    if not verdict:
        raise TryReviewError("verdict_unreadable", "The review finished but its verdict could not be read. Try again.")

    # Same normalisation as the production flow.
    if verdict.get("verdict") == "SAFE":
        verdict["findings"] = []
    elif verdict.get("verdict") == "RISKY" and not verdict.get("findings"):
        verdict["verdict"] = "SAFE"
        verdict["findings"] = []

    hunks = parse_diff_hunks(ctx.diff) if ctx.diff else _hunks_from_files(ctx.files)
    anchored, _unanchored = anchor_findings(verdict["findings"], hunks)
    anchored_ids = {id(f) for f in anchored}
    findings = [{**f, "in_diff": id(f) in anchored_ids} for f in verdict["findings"]]

    head_sha = (ctx.pr.get("head") or {}).get("sha") or "preview"
    return {
        "verdict": verdict["verdict"],
        "summary": verdict.get("summary", ""),
        "findings": findings,
        "markdown": render_review_body(
            verdict["verdict"], verdict.get("summary", ""), verdict["findings"], head_sha
        ),
        "source": ctx.source,
        "transparency": ctx.transparency,
        "scope_note": (
            "Diff-only review. Connect GitHub to add repository context and automatic reviews on "
            "every PR; connect monitoring and deploy tools to add live signals."
        ),
    }


def _hunks_from_files(files: List[Dict[str, Any]]) -> Dict[str, set]:
    """Anchor findings against per-file patches when no whole-PR diff was kept."""
    pieces = []
    for f in files:
        if f.get("patch"):
            name = f.get("filename", "")
            pieces.append(f"--- a/{name}\n+++ b/{name}\n{f['patch']}")
    return parse_diff_hunks("\n".join(pieces)) if pieces else {}

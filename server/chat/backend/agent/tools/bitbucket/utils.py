"""Shared utilities for Bitbucket agent tools."""

import json
import logging
import re
from typing import Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context, get_credentials_from_db
from connectors.bitbucket_connector.api_client import BitbucketAPIClient
from connectors.bitbucket_connector.oauth_utils import refresh_token_if_needed
from utils.auth.token_management import store_tokens_in_db
from utils.secrets.secret_ref_utils import get_token_owner_id
from utils.auth.command_gate import gate_action
from chat.backend.agent.utils.tool_output_cap import PASS_THROUGH_CHARS as _PASS_THROUGH_CHARS

logger = logging.getLogger(__name__)

DIFF_TRUNCATE_LIMIT = 50_000

# Budget for one page of file content, measured on the JSON-ESCAPED form of
# the text (what actually lands in the serialized tool output). Derived from
# tool_output_cap.PASS_THROUGH_CHARS with a 10K margin for the JSON envelope,
# so a file read is returned verbatim and never replaced by an LLM summary —
# even if the pass-through threshold is later lowered.
PAGE_CONTENT_BUDGET = min(30_000, _PASS_THROUGH_CHARS - 10_000)

_SHA40_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
# Mirrors api_client._COMMIT_SHA_RE: what _resolve_commit passes through as-is.
_SHA_LIKE_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)


def is_full_commit_sha(value) -> bool:
    """True when ``value`` is a full 40-hex commit SHA (not a ref name)."""
    return isinstance(value, str) and bool(_SHA40_RE.fullmatch(value))


def is_commit_sha_like(value) -> bool:
    """True when ``value`` looks like a (possibly abbreviated) commit SHA."""
    return isinstance(value, str) and bool(_SHA_LIKE_RE.fullmatch(value))


def get_bb_client_for_user(user_id: str):
    """Get a BitbucketAPIClient with auto-refreshed OAuth tokens.

    Returns:
        BitbucketAPIClient instance, or None if not connected.
    """
    try:
        bb_creds = get_credentials_from_db(user_id, "bitbucket")
        if not bb_creds:
            return None

        auth_type = bb_creds.get("auth_type", "oauth")
        access_token = bb_creds.get("access_token")
        if not access_token:
            return None

        # Refresh OAuth tokens if needed
        if auth_type == "oauth":
            old_access_token = access_token
            bb_creds = refresh_token_if_needed(bb_creds)
            access_token = bb_creds.get("access_token", access_token)

            # Persist refreshed token if changed
            if access_token != old_access_token:
                try:
                    owner_id = get_token_owner_id(user_id, "bitbucket")
                    store_tokens_in_db(owner_id, bb_creds, "bitbucket")
                    logger.info("Persisted refreshed Bitbucket token")
                except Exception as e:
                    logger.warning(f"Failed to persist refreshed Bitbucket token: {e}")

        email = bb_creds.get("email")
        return BitbucketAPIClient(access_token, auth_type=auth_type, email=email)

    except Exception as e:
        logger.error(f"Failed to get Bitbucket client: {e}", exc_info=True)
        return None


def is_bitbucket_connected(user_id: str) -> bool:
    """Check if Bitbucket credentials exist for a user."""
    try:
        creds = get_credentials_from_db(user_id, "bitbucket")
        return bool(creds and creds.get("access_token"))
    except Exception as e:
        logger.warning(f"Error checking Bitbucket connection: {e}")
        return False


def get_default_branch(user_id: str, workspace: str, repo_slug: str) -> Optional[str]:
    """Look up the default branch for a connected Bitbucket repo."""
    try:
        full_name = f"{workspace}/{repo_slug}"
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketTools:branch]")
                cur.execute(
                    "SELECT default_branch FROM connected_repos WHERE provider = 'bitbucket' AND repo_full_name = %s LIMIT 1",
                    (full_name,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception as e:
        logger.warning(f"Failed to look up default branch for {workspace}/{repo_slug}: {e}")
    return None


def _escaped_len(text: str) -> int:
    """Length of ``text`` once JSON-escaped inside a serialized string."""
    return len(json.dumps(text)) - 2


def _slice_to_escaped_budget(text: str, budget: int) -> str:
    """Longest prefix of ``text`` whose JSON-escaped length fits ``budget``."""
    # Escaped length >= raw length, so no prefix longer than ``budget`` raw
    # chars can ever fit — capping ``hi`` keeps the binary-search probes small.
    lo, hi = 0, min(len(text), budget)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _escaped_len(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    # Never return an empty slice: paging must always make progress.
    return text[: max(lo, 1)]


def page_file_content(content: str, start_line: int = 1, start_char: int = 0,
                      ref: Optional[str] = None) -> str:
    """Return a verbatim page of ``content`` starting at ``start_line``.

    The page is sized so its JSON-escaped form stays under
    PAGE_CONTENT_BUDGET, which keeps the serialized tool output under the
    pass-through summarizer threshold — file reads are never summarized. A
    file that fits in one page from line 1 is returned unchanged (no header).
    Otherwise the page is prefixed with a header line:

        [lines X-Y of N — pass start_line=<Y+1> to continue]

    A single line longer than the budget is sliced and continued WITHIN the
    line via ``start_char``:

        [lines X-X of N — pass start_line=<X> start_char=<offset> to continue]

    When ``ref`` is given (the resolved commit SHA the content was read at),
    continue hints include ``commit=<ref>`` so follow-up pages are pinned to
    the same file version even if the branch tip moves mid-read.

    Concatenating the header-stripped pages reproduces the file
    byte-for-byte (each page carries its own line terminators).
    """
    start_line = max(start_line or 1, 1)
    start_char = max(start_char or 0, 0)

    # Raw length is a lower bound on escaped length, so only files that are
    # candidates for single-page return pay the whole-file escape check.
    if (start_line == 1 and start_char == 0
            and len(content) <= PAGE_CONTENT_BUDGET
            and _escaped_len(content) <= PAGE_CONTENT_BUDGET):
        return content

    lines = content.split("\n")
    total = len(lines)
    idx = start_line - 1
    if idx >= total:
        return f"[start_line {start_line} is past the end of the file ({total} lines)]"
    if start_char > len(lines[idx]):
        # A stale or invented hint: never return a page whose header claims
        # a range while silently dropping part of a line.
        return (
            f"[start_char {start_char} is past the end of line "
            f"{start_line} ({len(lines[idx])} chars)]"
        )

    ref_hint = f" commit={ref}" if ref else ""
    parts, last, continue_hint = _collect_page(lines, idx, start_char, ref_hint)
    return _page_header(start_line, last, total, continue_hint) + "".join(parts)


def _collect_page(lines: list, start_idx: int, start_char: int,
                  ref_hint: str) -> tuple[list[str], int, Optional[str]]:
    """Accumulate whole lines (with terminators) from ``start_idx`` until the
    escaped budget is spent. Returns ``(parts, last_line_1based,
    continue_hint)``; ``continue_hint`` is None at end of file."""
    total = len(lines)
    parts: list[str] = []
    used = 0
    idx = start_idx
    while idx < total:
        offset = start_char if idx == start_idx else 0
        line = lines[idx][offset:] if offset else lines[idx]
        piece = line + ("\n" if idx < total - 1 else "")
        cost = _escaped_len(piece)
        if used + cost > PAGE_CONTENT_BUDGET:
            if not parts:
                # A single line bigger than the whole budget: slice within it.
                sliced = _slice_to_escaped_budget(line, PAGE_CONTENT_BUDGET)
                hint = (
                    f"pass start_line={idx + 1} "
                    f"start_char={offset + len(sliced)}{ref_hint} to continue"
                )
                return [sliced], idx + 1, hint
            return parts, idx, f"pass start_line={idx + 1}{ref_hint} to continue"
        parts.append(piece)
        used += cost
        idx += 1
    return parts, total, None


def _page_header(first: int, last: int, total: int,
                 continue_hint: Optional[str]) -> str:
    tail = continue_hint or "end of file"
    return f"[lines {first}-{last} of {total} — {tail}]\n"


def apply_edits_checked(original: str, edits: list) -> tuple[Optional[str], Optional[str]]:
    """Apply anchored search-and-replace edits to ``original``.

    Wraps the shared replacer chain from github_fix_tool (exact through
    fuzzy matching, whole-file old_string ratio guard, overlap rejection)
    and additionally rejects no-op and empty/whitespace-only results.

    Returns ``(new_content, None)`` on success or ``(None, error)``.
    """
    from chat.backend.agent.tools.github_fix_tool import _apply_edits

    suggested, apply_err = _apply_edits(original, edits)
    if apply_err or suggested is None:
        return None, apply_err or "edit application failed"
    if suggested == original:
        return None, (
            "Applied edits produced no change to the file. "
            "Double-check old_string/new_string."
        )
    if not suggested.strip():
        return None, (
            "Applied edits produced an empty (or whitespace-only) file. If you "
            "really intend to empty this file, do it manually — anchored edits "
            "are for targeted code changes."
        )
    return suggested, None


def require_repo(ws: Optional[str], repo: Optional[str]) -> Optional[str]:
    """Return an error message if workspace or repo is missing, else None."""
    if not ws or not repo:
        return "workspace and repo_slug are required"
    return None


def forward_if_error(result) -> Optional[str]:
    """Return a JSON string if the result is an API error dict, else None."""
    if isinstance(result, dict) and result.get("error") is True:
        return json.dumps(result, default=str)
    return None


def truncate_text(text: str, limit: int, label: str = "output") -> str:
    """Truncate text to a maximum length with an informative suffix."""
    if len(text) <= limit:
        return text
    size_kb = limit // 1000
    return text[:limit] + f"\n... [{label} truncated at {size_kb}KB]"


def build_error_response(message: str, **kwargs) -> str:
    """Build a JSON error response string."""
    result = {"error": True, "message": message}
    result.update(kwargs)
    return json.dumps(result)


def build_success_response(**kwargs) -> str:
    """Build a JSON success response string."""
    result = {"success": True}
    result.update(kwargs)
    return json.dumps(result, default=str)


def build_cancelled_response() -> str:
    """Build the standard cancellation response for a rejected confirmation."""
    return build_success_response(message="Operation cancelled by user", cancelled=True)


def confirm_or_cancel(user_id: str, message: str, tool_name: str) -> Optional[str]:
    """Request human approval for a destructive action.

    Returns ``None`` if approved, or a JSON cancellation response string
    if the user declines. Delegates to the unified command gate so
    Bitbucket confirmations share the same UI/WS/taint plumbing as the
    shell-command gate.
    """
    if gate_action(user_id=user_id, tool_name=tool_name, summary=message).allowed:
        return None
    return build_cancelled_response()

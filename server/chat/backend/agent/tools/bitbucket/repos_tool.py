"""Bitbucket repository, file, and code operations tool."""

import json
import logging
import re
import threading
import time
from typing import Literal, Optional

try:
    # Supports a true per-call timeout for the find_in_file regex fallback.
    # Transitively present via tiktoken; the daemon-thread deadline below
    # covers its absence.
    import regex as _regex_mod
except ImportError:  # pragma: no cover — regex ships with tiktoken
    _regex_mod = None

from pydantic import BaseModel, Field

from .utils import (
    get_bb_client_for_user,
    get_default_branch,
    require_repo,
    forward_if_error,
    build_error_response,
    build_success_response,
    confirm_or_cancel,
    page_file_content,
    apply_edits_checked,
    is_full_commit_sha,
)
from .fix_tool import FixEdit

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


class BitbucketReposArgs(BaseModel):
    action: Literal[
        "list_repos",
        "get_repo",
        "get_file_contents",
        "find_in_file",
        "edit_file",
        "create_or_update_file",
        "delete_file",
        "get_directory_tree",
        "search_code",
        "list_workspaces",
        "get_workspace",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug (required for repo-scoped actions).")
    repo_slug: Optional[str] = Field(None, description="Repository slug (required for repo-scoped actions).")
    path: Optional[str] = Field(None, description="File or directory path (for file/directory operations).")
    content: Optional[str] = Field(None, description="File content (for create_or_update_file).")
    message: Optional[str] = Field(None, description="Commit message (for edit_file, create_or_update_file, delete_file).")
    branch: Optional[str] = Field(None, description="Branch name (for file operations). Defaults to saved branch — except edit_file, which requires an explicit branch (create one first, then open a PR).")
    commit: Optional[str] = Field(None, description="Commit hash or branch ref (for get_file_contents, find_in_file, get_directory_tree). Defaults to HEAD.")
    query: Optional[str] = Field(None, description="Search query (for search_code) or pattern (for find_in_file — matched as a literal substring first, then as a regex).")
    start_line: Optional[int] = Field(
        None,
        description=(
            "For get_file_contents: 1-based line to start reading from. Large "
            "files are returned in verbatim pages; follow the continue hint in "
            "the page header to read further."
        ),
    )
    start_char: Optional[int] = Field(
        None,
        description=(
            "For get_file_contents: character offset within start_line, used to "
            "continue an oversized single line (copy it from the page header's "
            "continue hint)."
        ),
    )
    edits: Optional[list[FixEdit]] = Field(
        None,
        description=(
            "For edit_file: list of anchored search-and-replace edits "
            "({old_string, new_string, replace_all}). Copy old_string exactly "
            "from get_file_contents output and keep it narrow — the changed "
            "lines plus 1-3 lines of context, never the whole file."
        ),
    )


_FIND_MAX_MATCHES = 50
_FIND_LINE_TRUNCATE = 200
_FIND_REGEX_TIMEOUT_S = 2.0


class _RegexSearchTimeout(Exception):
    """Raised when a find_in_file regex scan exceeds its deadline."""


class _InvalidFindQuery(Exception):
    """Raised when a find_in_file query has no literal matches AND does not
    compile as a regex — a bad argument, not a genuine zero-match result."""


def _invalid_query_message(query: str, exc) -> str:
    return (
        f"No literal matches for {query!r}, and it does not compile as a "
        f"regex ({exc}). Fix the pattern, or search for an exact code snippet "
        "copied from the file."
    )


def _scan_lines(lines: list, matches) -> tuple[list[str], int]:
    """Scan ``lines`` with predicate ``matches``; format up to the cap."""
    formatted: list[str] = []
    total = 0
    for line_no, line in enumerate(lines, 1):
        if matches(line):
            total += 1
            if len(formatted) < _FIND_MAX_MATCHES:
                text = line if len(line) <= _FIND_LINE_TRUNCATE else line[:_FIND_LINE_TRUNCATE] + "..."
                formatted.append(f"L{line_no}: {text}")
    return formatted, total


def _regex_timeout_message(query: str) -> str:
    return (
        f"Regex search for {query!r} timed out after "
        f"{_FIND_REGEX_TIMEOUT_S:.0f}s (catastrophic backtracking). "
        "Retry with a literal code snippet or a simpler pattern."
    )


def _regex_scan_with_module_timeout(lines: list, query: str) -> tuple[list[str], int]:
    """Regex fallback scan using the ``regex`` module's native per-call
    timeout — a genuinely cancellable boundary: the engine checks the
    deadline DURING matching, so a catastrophic pattern stops burning CPU
    at the deadline instead of running to completion in an abandoned
    thread."""
    try:
        pattern = _regex_mod.compile(query)
    except _regex_mod.error as exc:
        raise _InvalidFindQuery(_invalid_query_message(query, exc)) from None

    deadline = time.monotonic() + _FIND_REGEX_TIMEOUT_S

    def matches(line: str) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return pattern.search(line, timeout=remaining) is not None

    try:
        return _scan_lines(lines, matches)
    except TimeoutError:
        raise _RegexSearchTimeout(_regex_timeout_message(query)) from None


def _regex_scan_with_thread_deadline(lines: list, query: str) -> tuple[list[str], int]:
    """Stdlib fallback when the ``regex`` module is absent. A daemon thread
    (NOT concurrent.futures: its non-daemon workers are joined at
    interpreter exit, so an abandoned catastrophic scan would block process
    shutdown). On timeout the runaway thread is abandoned — it burns CPU
    until the scan finishes but never blocks exit, and it holds only this
    call's line list."""
    try:
        pattern = re.compile(query)
    except re.error as exc:
        raise _InvalidFindQuery(_invalid_query_message(query, exc)) from None

    outcome: dict = {}

    def _regex_scan():
        try:
            outcome["result"] = _scan_lines(
                lines, lambda line: pattern.search(line) is not None
            )
        except Exception as exc:  # pragma: no cover — defensive
            outcome["error"] = exc

    worker = threading.Thread(target=_regex_scan, daemon=True, name="bb-find-regex")
    worker.start()
    worker.join(timeout=_FIND_REGEX_TIMEOUT_S)
    if "result" in outcome:
        return outcome["result"]
    if "error" in outcome:
        raise outcome["error"]
    raise _RegexSearchTimeout(_regex_timeout_message(query))


def _find_in_content(file_content: str, query: str) -> tuple[list[str], int]:
    """Return (formatted match lines, total match count) for ``query``.

    Literal substring matching runs first: it is exact (no regex misreads of
    code like ``arr[0]`` or ``foo.bar``) and immune to backtracking. Only
    when the query never occurs literally is it tried as a regex, under a
    hard deadline: via the ``regex`` module's native timeout when available
    (truly cancellable mid-match), else a daemon-thread deadline. Both raise
    _RegexSearchTimeout on overrun; a query that also fails to compile
    raises _InvalidFindQuery (a bad argument, distinct from a genuine
    zero-match result). At most _FIND_MAX_MATCHES lines are
    formatted as ``L<line_no>: <line>`` (truncated), so the output stays
    small.
    """
    lines = file_content.split("\n")
    formatted, total = _scan_lines(lines, lambda line: query in line)
    if total:
        return formatted, total

    if _regex_mod is not None:
        return _regex_scan_with_module_timeout(lines, query)
    return _regex_scan_with_thread_deadline(lines, query)


def bitbucket_repos(
    action: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
    path: Optional[str] = None,
    content: Optional[str] = None,
    message: Optional[str] = None,
    branch: Optional[str] = None,
    commit: Optional[str] = None,
    query: Optional[str] = None,
    start_line: Optional[int] = None,
    start_char: Optional[int] = None,
    edits: Optional[list] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User context not available")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    ws, repo = workspace, repo_slug

    # edit_file is deliberately absent: it REQUIRES an explicit branch (see
    # its handler) — auto-filling the saved/default branch here would let an
    # agent that omits `branch` commit straight to the default branch.
    branch_defaulted = action in (
        "get_repo", "get_file_contents", "find_in_file",
        "create_or_update_file", "delete_file", "get_directory_tree",
    )
    if branch_defaulted and ws and repo:
        if not branch:
            branch = get_default_branch(user_id, ws, repo)

    try:
        if action == "list_workspaces":
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[BitbucketRepos:workspaces]")
                    cur.execute(
                        """SELECT DISTINCT split_part(repo_full_name, '/', 1) AS workspace
                           FROM connected_repos
                           WHERE provider = 'bitbucket'
                             AND repo_full_name LIKE '%%/%%'
                           ORDER BY workspace""",
                    )
                    rows = cur.fetchall()
            if not rows:
                return build_success_response(
                    workspaces=[], count=0,
                    message="No workspaces connected. The user must select repos in the Bitbucket connector settings.",
                )
            workspaces = [{"slug": r[0], "name": r[0]} for r in rows]
            return build_success_response(workspaces=workspaces, count=len(workspaces))

        if action == "get_workspace":
            if not ws:
                return build_error_response("workspace is required")
            return json.dumps(client.get_workspace(ws), default=str)

        if action == "list_repos":
            if not ws:
                return build_error_response("workspace is required")
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[BitbucketRepos:list]")
                    cur.execute(
                        """SELECT repo_full_name, default_branch, is_private,
                                  metadata_summary, metadata_status
                           FROM connected_repos
                           WHERE provider = 'bitbucket'
                             AND repo_full_name LIKE %s
                           ORDER BY repo_full_name""",
                        (ws + "/%",),
                    )
                    rows = cur.fetchall()
            if not rows:
                return build_success_response(
                    repositories=[], count=0, workspace=ws,
                    message="No repos connected for this workspace. The user must select repos in the Bitbucket connector settings.",
                )
            repos = []
            for r in rows:
                full_name = r[0]
                slug = full_name.split("/", 1)[1] if "/" in full_name else full_name
                repos.append({
                    "slug": slug,
                    "full_name": full_name,
                    "is_private": r[2],
                    "description": r[3] or ("(generating...)" if r[4] != 'ready' else "(no description)"),
                    "mainbranch": r[1],
                })
            return build_success_response(repositories=repos, count=len(repos), workspace=ws)

        if action == "get_repo":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            return json.dumps(client.get_repository(ws, repo), default=str)

        if action == "get_file_contents":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            ref = commit or branch or "HEAD"
            result = client.get_file_contents(ws, repo, path, commit=ref)
            if err := forward_if_error(result):
                return err
            if isinstance(result, dict) and isinstance(result.get("content"), str):
                result = dict(result)
                # Pin continue hints to the resolved commit so follow-up
                # pages read the same file version even if the branch moves.
                read_commit = result.get("commit")
                result["content"] = page_file_content(
                    result["content"], start_line or 1, start_char or 0,
                    ref=read_commit if is_full_commit_sha(read_commit) else None,
                )
            return json.dumps(result, default=str)

        if action == "find_in_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if not query:
                return build_error_response("query is required")
            ref = commit or branch or "HEAD"
            result = client.get_file_contents(ws, repo, path, commit=ref)
            if err := forward_if_error(result):
                return err
            file_content = result.get("content") if isinstance(result, dict) else None
            if not isinstance(file_content, str):
                return build_error_response(f"'{path}' is not a readable file")
            try:
                matches, total = _find_in_content(file_content, query)
            except (_RegexSearchTimeout, _InvalidFindQuery) as exc:
                return build_error_response(str(exc))
            read_commit = result.get("commit")
            pin = f" commit={read_commit}" if is_full_commit_sha(read_commit) else ""
            return build_success_response(
                path=path,
                query=query,
                commit=read_commit,
                total_matches=total,
                shown=len(matches),
                matches=matches,
                hint=(
                    "Jump to a match with get_file_contents "
                    f"start_line=<line number>{pin}."
                ),
            )

        if action == "edit_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if not edits:
                return build_error_response("edits is required (list of {old_string, new_string} edits)")
            if not message:
                return build_error_response("message (commit message) is required")
            if not branch:
                return build_error_response("branch is required")
            fetched = client.get_file_contents(ws, repo, path, commit=branch)
            if err := forward_if_error(fetched):
                return err
            original = fetched.get("content") if isinstance(fetched, dict) else None
            if not isinstance(original, str):
                return build_error_response(
                    f"Could not read '{path}' on branch '{branch}' — it may be a directory. "
                    "edit_file only works on existing files; use create_or_update_file for new files."
                )
            # The compare-and-swap below is only real if the read was pinned
            # to an actual commit SHA. When ref resolution fell back (the
            # envelope echoes the raw branch name), fail closed instead of
            # sending Bitbucket a non-SHA `parents` (400) or silently
            # voiding the CAS.
            read_commit = fetched.get("commit")
            if not is_full_commit_sha(read_commit):
                return build_error_response(
                    f"Could not pin the current tip of branch '{branch}' for a safe "
                    "compare-and-swap commit (ref resolution failed). Retry the edit; "
                    "if it persists, verify the branch name."
                )
            new_content, apply_err = apply_edits_checked(original, edits)
            if apply_err or new_content is None:
                return build_error_response(apply_err or "edit application failed")
            if cancelled := confirm_or_cancel(user_id,
                    f"Commit edit to '{path}' on branch '{branch}' in {ws}/{repo}",
                    "bitbucket:commit_file"):
                return cancelled
            # parents = the commit the file was read at → compare-and-swap:
            # Bitbucket rejects the write if the branch tip moved since the read.
            result = client.create_or_update_file(
                ws, repo, path, new_content, message, branch,
                parents=read_commit,
            )
            if err := forward_if_error(result):
                return err
            return build_success_response(
                message=f"Applied {len(edits)} edit(s) to '{path}' on {branch}",
                edits_applied=len(edits),
                result=result,
            )

        if action == "create_or_update_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if content is None:
                return build_error_response("content is required")
            if not message:
                return build_error_response("message (commit message) is required")
            if not branch:
                return build_error_response("branch is required")
            if cancelled := confirm_or_cancel(user_id,
                    f"Commit file '{path}' to branch '{branch}' in {ws}/{repo}",
                    "bitbucket:commit_file"):
                return cancelled
            result = client.create_or_update_file(ws, repo, path, content, message, branch)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"File '{path}' committed to {branch}", result=result)

        if action == "delete_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if not message:
                return build_error_response("message (commit message) is required")
            if not branch:
                return build_error_response("branch is required")
            if cancelled := confirm_or_cancel(user_id,
                    f"Delete file '{path}' from branch '{branch}' in {ws}/{repo}",
                    "bitbucket:delete_file"):
                return cancelled
            result = client.delete_file(ws, repo, path, message, branch)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"File '{path}' deleted from {branch}")

        if action == "get_directory_tree":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            ref = commit or branch or "HEAD"
            return json.dumps(client.get_directory_tree(ws, repo, path or "", commit=ref), default=str)

        if action == "search_code":
            if not ws:
                return build_error_response("workspace is required")
            if not query:
                return build_error_response("query is required")
            return json.dumps(client.search_code(ws, query), default=str)

        return build_error_response(f"Unknown action: {action}")

    except Exception as e:
        logger.error(f"Bitbucket repos tool error: {e}", exc_info=True)
        return build_error_response(f"Bitbucket API error: {str(e)}")

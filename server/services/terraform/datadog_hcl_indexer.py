"""
Terraform HCL indexer for Datadog resources.

Walks .tf files in a connected GitHub repo via MCP, parses with python-hcl2,
and extracts every `datadog_monitor`, `datadog_downtime`, and
`datadog_downtime_schedule` block into `terraform_datadog_resources`.

Read-only: never mutates the repo.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

TF_DATADOG_RESOURCE_TYPES = (
    "datadog_monitor",
    "datadog_downtime",
    "datadog_downtime_schedule",
)

FUZZY_MATCH_MIN_SCORE = 0.6
_FUZZY_AMBIGUITY_DELTA = 0.05

MatchConfidence = Literal["exact_query", "exact_name", "fuzzy_name", "none"]

_RESOURCE_HEADER_RE = re.compile(
    r'^\s*resource\s+"(?P<type>datadog_(?:monitor|downtime|downtime_schedule))"\s+"(?P<name>[A-Za-z0-9_\-]+)"\s*\{',
    re.MULTILINE,
)


@dataclass
class IndexedResource:
    resource_type: str
    resource_address: str  # e.g. datadog_monitor.api_error_rate
    file_path: str
    line_start: int
    line_end: int
    monitor_name: Optional[str] = None
    query_hash: Optional[str] = None
    scope: Optional[str] = None
    silenced_inline: Optional[Dict[str, Any]] = None
    raw_block: str = ""
    commit_sha: Optional[str] = None
    # downtime-specific
    downtime_monitor_ref: Optional[str] = None  # e.g. datadog_monitor.foo.id or raw id


@dataclass
class IndexSummary:
    repo_full_name: str
    commit_sha: Optional[str]
    files_scanned: int = 0
    files_skipped: int = 0
    resources_indexed: int = 0
    resources: List[IndexedResource] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def index_repo(user_id: str, repo_full_name: str, branch: Optional[str] = None) -> IndexSummary:
    """Walk the repo's default (or specified) branch and build a fresh index.

    Persists rows to `terraform_datadog_resources` for `user_id` + `repo_full_name`
    (replaces prior rows for that pair in a single transaction).
    """
    owner, repo = _split_repo(repo_full_name)
    summary = IndexSummary(repo_full_name=repo_full_name, commit_sha=None)

    if not owner or not repo:
        summary.errors.append(f"Invalid repo format: {repo_full_name}")
        return summary

    effective_branch = branch or get_repo_default_branch(user_id, repo_full_name) or "main"
    summary.commit_sha = _get_branch_head_sha(user_id, owner, repo, effective_branch)

    tf_paths = _walk_tf_files(user_id, owner, repo, effective_branch)
    for path in tf_paths:
        content = _fetch_file(user_id, owner, repo, path, effective_branch)
        if content is None:
            summary.files_skipped += 1
            continue
        summary.files_scanned += 1
        try:
            extracted = extract_resources_from_hcl(content, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HCL_INDEXER] Parse error in %s: %s", path, exc)
            summary.errors.append(f"{path}: {exc}")
            continue
        for res in extracted:
            res.commit_sha = summary.commit_sha
            summary.resources.append(res)

    summary.resources_indexed = len(summary.resources)
    # An incomplete scan would shrink or blank out a previously-good index —
    # keep the old index in place and surface the errors to the caller.
    if summary.errors or summary.files_skipped:
        logger.warning(
            "[HCL_INDEXER] Skipping persist for %s: errors=%d skipped=%d",
            repo_full_name,
            len(summary.errors),
            summary.files_skipped,
        )
        return summary
    _persist_index(user_id, repo_full_name, summary.resources)
    return summary


def extract_resources_from_hcl(content: str, file_path: str) -> List[IndexedResource]:
    """Parse a single .tf file's content and return IndexedResource rows.

    python-hcl2 gives us the semantic block tree; we use a regex over the raw
    text to recover approximate line ranges, which are good enough for user-
    facing tool output.
    """
    import hcl2  # deferred import so module import doesn't require the dep

    try:
        parsed = hcl2.load(io.StringIO(content))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"HCL parse failed: {exc}") from exc

    line_map = _build_line_map(content)
    resources = parsed.get("resource", []) or []

    results: List[IndexedResource] = []
    for resource_block in resources:
        if not isinstance(resource_block, dict):
            continue
        for rtype_raw, named_blocks in resource_block.items():
            rtype = _unquote(rtype_raw)
            if rtype not in TF_DATADOG_RESOURCE_TYPES:
                continue
            if not isinstance(named_blocks, dict):
                continue
            for name_raw, body in named_blocks.items():
                name = _unquote(name_raw)
                if not isinstance(body, dict):
                    continue
                address = f"{rtype}.{name}"
                line_start, line_end, raw = _lookup_line_range(line_map, rtype, name, content)
                row = IndexedResource(
                    resource_type=rtype,
                    resource_address=address,
                    file_path=file_path,
                    line_start=line_start,
                    line_end=line_end,
                    raw_block=raw,
                )
                if rtype == "datadog_monitor":
                    row.monitor_name = _clean_scalar(body.get("name"))
                    row.query_hash = _query_hash(_clean_scalar(body.get("query")))
                    options = _first(body.get("options"))
                    if isinstance(options, dict):
                        silenced = _first(options.get("silenced"))
                        if silenced is not None:
                            row.silenced_inline = silenced if isinstance(silenced, dict) else {"_raw": silenced}
                elif rtype == "datadog_downtime":
                    row.scope = _stringify_scope(body.get("scope"))
                    row.downtime_monitor_ref = _clean_scalar(body.get("monitor_id"))
                    tags = _first(body.get("monitor_tags"))
                    if tags and not row.scope:
                        row.scope = _stringify_scope(tags)
                elif rtype == "datadog_downtime_schedule":
                    row.scope = _stringify_scope(body.get("scope"))
                    mid = _first(body.get("monitor_identifier"))
                    if isinstance(mid, dict):
                        row.downtime_monitor_ref = _clean_scalar(mid.get("monitor_id"))
                results.append(row)
    return results


def match_resources_to_monitor(
    resources: Iterable[IndexedResource],
    monitor_name: str,
    monitor_query: Optional[str],
) -> Tuple[Optional[IndexedResource], MatchConfidence]:
    """Find best-matching datadog_monitor resource for a live monitor.

    Query-hash matches short-circuit since they're canonical. Name-based matches
    fail closed on ambiguity (multiple exact hits, or fuzzy near-ties) to avoid
    silently writing a downtime against the wrong monitor.
    """
    monitors = [r for r in resources if r.resource_type == "datadog_monitor"]
    query_hash = _query_hash(monitor_query) if monitor_query else None
    if query_hash:
        for r in monitors:
            if r.query_hash and r.query_hash == query_hash:
                return r, "exact_query"
    if not monitor_name:
        return None, "none"

    target = monitor_name.strip().lower()
    exact = [r for r in monitors if (r.monitor_name or "").strip().lower() == target]
    if len(exact) == 1:
        return exact[0], "exact_name"
    if len(exact) > 1:
        return None, "none"

    scored: List[Tuple[float, IndexedResource]] = []
    for r in monitors:
        candidate = (r.monitor_name or "").strip().lower()
        if not candidate:
            continue
        score = _similarity(candidate, target)
        if score >= FUZZY_MATCH_MIN_SCORE:
            scored.append((score, r))
    if not scored:
        return None, "none"
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top_resource = scored[0]
    # Fail closed when the runner-up is within a near-tie of the leader.
    if len(scored) > 1 and (top_score - scored[1][0]) < _FUZZY_AMBIGUITY_DELTA:
        return None, "none"
    return top_resource, "fuzzy_name"


def load_index(
    user_id: str,
    repo_full_name: Optional[str] = None,
    include_raw_block: bool = False,
) -> List[IndexedResource]:
    """Load all indexed resources for a user, optionally filtered by repo.

    `raw_block` can inflate rows significantly for large monitors; callers that
    only need match metadata should leave `include_raw_block=False`.
    """
    from utils.db.connection_pool import db_pool

    raw_column = "raw_block" if include_raw_block else "''"
    sql = f"""
        SELECT resource_type, resource_address, file_path, line_start, line_end,
               monitor_name, query_hash, scope, silenced_inline, {raw_column}, commit_sha
        FROM terraform_datadog_resources
        WHERE user_id = %s
    """
    params: Tuple[Any, ...] = (user_id,)
    if repo_full_name:
        sql += " AND repo_full_name = %s"
        params = (user_id, repo_full_name)

    rows: List[IndexedResource] = []
    try:
        with db_pool.get_admin_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            for row in cur.fetchall():
                silenced = row[8]
                if isinstance(silenced, str):
                    try:
                        silenced = json.loads(silenced)
                    except json.JSONDecodeError:
                        silenced = None
                rows.append(
                    IndexedResource(
                        resource_type=row[0],
                        resource_address=row[1],
                        file_path=row[2],
                        line_start=row[3] or 0,
                        line_end=row[4] or 0,
                        monitor_name=row[5],
                        query_hash=row[6],
                        scope=row[7],
                        silenced_inline=silenced,
                        raw_block=row[9] or "",
                        commit_sha=row[10],
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.error("[HCL_INDEXER] load_index failed: %s", exc)
    return rows


def count_indexed_resources(user_id: str, repo_full_name: str) -> int:
    """Cheap row-count without hydrating every resource."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM terraform_datadog_resources WHERE user_id = %s AND repo_full_name = %s",
                (user_id, repo_full_name),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("[HCL_INDEXER] count failed: %s", exc)
        return 0


def get_repo_default_branch(user_id: str, repo_full_name: str) -> Optional[str]:
    """Lookup of `github_connected_repos.default_branch` shared with callers."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT default_branch FROM github_connected_repos WHERE user_id = %s AND repo_full_name = %s",
                (user_id, repo_full_name),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[HCL_INDEXER] Default branch lookup failed: %s", exc)
        return None




# ---------------------------------------------------------------------------
# GitHub helpers (MCP reads only)
# ---------------------------------------------------------------------------


def _split_repo(repo_full_name: str) -> Tuple[Optional[str], Optional[str]]:
    if not repo_full_name or "/" not in repo_full_name:
        return None, None
    owner, _, repo = repo_full_name.partition("/")
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return None, None
    return owner, repo


def _get_branch_head_sha(user_id: str, owner: str, repo: str, branch: str) -> Optional[str]:
    from chat.backend.agent.tools.github_mcp_utils import call_github_mcp_sync, parse_mcp_response

    result = call_github_mcp_sync(
        "get_branch",
        {"owner": owner, "repo": repo, "branch": branch},
        user_id,
    )
    parsed = parse_mcp_response(result)
    commit = parsed.get("commit") if isinstance(parsed, dict) else None
    if isinstance(commit, dict):
        return commit.get("sha") or (commit.get("commit") or {}).get("sha")
    return parsed.get("sha") if isinstance(parsed, dict) else None


def _walk_tf_files(user_id: str, owner: str, repo: str, branch: str) -> List[str]:
    """Recursive walk via GitHub MCP `get_file_contents`. Returns .tf paths."""
    from chat.backend.agent.tools.github_mcp_utils import call_github_mcp_sync, parse_mcp_response

    out: List[str] = []
    stack: List[str] = [""]
    visited: set = set()
    while stack:
        path = stack.pop()
        if path in visited:
            continue
        visited.add(path)

        args = {"owner": owner, "repo": repo, "path": path, "ref": f"refs/heads/{branch}"}
        result = call_github_mcp_sync("get_file_contents", args, user_id)
        parsed = parse_mcp_response(result)
        entries = _extract_directory_entries(parsed)
        if entries is None:
            continue
        for entry in entries:
            etype = entry.get("type")
            epath = entry.get("path") or entry.get("name")
            if not epath:
                continue
            if etype == "dir":
                stack.append(epath)
            elif etype == "file" and epath.endswith(".tf"):
                out.append(epath)
    return out


def _extract_directory_entries(parsed: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        for key in ("entries", "content", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [e for e in value if isinstance(e, dict)]
    return None


def _fetch_file(user_id: str, owner: str, repo: str, path: str, branch: str) -> Optional[str]:
    from chat.backend.agent.tools.github_mcp_utils import call_github_mcp_sync, parse_file_content_response

    args = {"owner": owner, "repo": repo, "path": path, "ref": f"refs/heads/{branch}"}
    result = call_github_mcp_sync("get_file_contents", args, user_id)
    return parse_file_content_response(result)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_index(user_id: str, repo_full_name: str, resources: List[IndexedResource]) -> None:
    from psycopg2.extras import execute_values
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM terraform_datadog_resources WHERE user_id = %s AND repo_full_name = %s",
                (user_id, repo_full_name),
            )
            if resources:
                rows = [
                    (
                        user_id,
                        repo_full_name,
                        r.commit_sha,
                        r.resource_type,
                        r.resource_address,
                        r.file_path,
                        r.line_start,
                        r.line_end,
                        r.monitor_name,
                        r.query_hash,
                        r.scope,
                        json.dumps(r.silenced_inline) if r.silenced_inline is not None else None,
                        r.raw_block,
                    )
                    for r in resources
                ]
                execute_values(
                    cur,
                    """
                    INSERT INTO terraform_datadog_resources
                    (user_id, repo_full_name, commit_sha, resource_type, resource_address,
                     file_path, line_start, line_end, monitor_name, query_hash, scope,
                     silenced_inline, raw_block)
                    VALUES %s
                    ON CONFLICT (user_id, repo_full_name, resource_address, file_path) DO UPDATE
                    SET commit_sha = EXCLUDED.commit_sha,
                        resource_type = EXCLUDED.resource_type,
                        line_start = EXCLUDED.line_start,
                        line_end = EXCLUDED.line_end,
                        monitor_name = EXCLUDED.monitor_name,
                        query_hash = EXCLUDED.query_hash,
                        scope = EXCLUDED.scope,
                        silenced_inline = EXCLUDED.silenced_inline,
                        raw_block = EXCLUDED.raw_block,
                        indexed_at = CURRENT_TIMESTAMP
                    """,
                    rows,
                )
            conn.commit()
    except Exception:
        logger.exception("[HCL_INDEXER] Persist failed for %s", repo_full_name)
        raise


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _first(value: Any) -> Any:
    """python-hcl2 returns most fields as single-element lists. Unwrap."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _unquote(s: Any) -> Any:
    """python-hcl2 ≥5 returns identifiers and string literals with surrounding quotes."""
    if isinstance(s, str) and len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _clean_scalar(value: Any) -> Any:
    return _unquote(_first(value))


def _query_hash(query: Any) -> Optional[str]:
    q = _unquote(_first(query))
    if not isinstance(q, str) or not q.strip():
        return None
    return hashlib.sha1(q.strip().encode("utf-8")).hexdigest()


def _stringify_scope(scope: Any) -> Optional[str]:
    s = _first(scope)
    if s is None:
        return None
    if isinstance(s, list):
        return ",".join(_unquote(str(x)) for x in s)
    return _unquote(str(s))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def _build_line_map(content: str) -> List[Tuple[int, int, str, str]]:
    """Return list of (start_line, end_line, type, name) by scanning headers.

    End line is inferred by brace matching from the header onward. This is a
    lightweight approximation; good enough for user-facing line ranges.
    """
    entries: List[Tuple[int, int, str, str]] = []
    for match in _RESOURCE_HEADER_RE.finditer(content):
        rtype = match.group("type")
        name = match.group("name")
        start_offset = match.start()
        header_line = content.count("\n", 0, start_offset) + 1
        end_line = _find_block_end_line(content, match.end())
        entries.append((header_line, end_line, rtype, name))
    return entries


def _lookup_line_range(
    line_map: List[Tuple[int, int, str, str]],
    rtype: str,
    name: str,
    content: str,
) -> Tuple[int, int, str]:
    for start, end, t, n in line_map:
        if t == rtype and n == name:
            lines = content.splitlines()
            raw = "\n".join(lines[start - 1 : end])
            return start, end, raw
    return 0, 0, ""


def _find_block_end_line(content: str, header_end_offset: int) -> int:
    """Brace-balance forward from the opening `{` to locate the closing `}`."""
    depth = 1  # header already consumed the opening `{`
    i = header_end_offset
    n = len(content)
    in_str = False
    str_char = ""
    while i < n and depth > 0:
        ch = content[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == str_char:
                in_str = False
        else:
            if ch in ('"', "'"):
                in_str = True
                str_char = ch
            elif ch == "#":
                nl = content.find("\n", i)
                if nl == -1:
                    break
                i = nl
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return content.count("\n", 0, i) + 1
        i += 1
    return content.count("\n", 0, min(i, n - 1)) + 1

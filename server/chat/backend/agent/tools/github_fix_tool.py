"""
GitHub Fix Tool - Suggest code fixes during RCA.

The LLM proposes one or more anchored search-and-replace edits. The server
fetches the current file from GitHub, applies the edits, stores the resulting
full file body as the suggestion, and the user reviews + creates the PR from
the UI.

Replacer chain (exact → fuzzier) is ported from opencode's edit tool, which
in turn credits cline and gemini-cli. Original source:
    https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/edit.ts
Licensed under MIT — see opencode's LICENSE for terms. This file ports the
replacer algorithms (LineTrimmed, BlockAnchor, WhitespaceNormalized,
IndentationFlexible, EscapeNormalized, TrimmedBoundary, ContextAware,
MultiOccurrence) and the Levenshtein-based similarity scoring to Python.
"""

import logging
from typing import Optional, Callable, Generator
from pydantic import BaseModel, Field

from .github_mcp_utils import (
    call_github_mcp_sync,
    parse_file_content_response,
    build_error_response,
    build_success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Args schema
# ---------------------------------------------------------------------------


class FixEdit(BaseModel):
    """A single anchored search-and-replace edit."""
    old_string: str = Field(
        description=(
            "Exact text to match in the current file. Include enough surrounding "
            "context (typically 1-3 lines above and below the change) to make the "
            "match unique. Whitespace counts; if you copy from get_file_contents "
            "the indentation will be right."
        )
    )
    new_string: str = Field(
        description="Replacement text. Indentation must match what belongs at that location."
    )
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence of old_string. Default False requires exactly one match.",
    )


class GitHubFixArgs(BaseModel):
    """Arguments for github_fix tool."""
    file_path: str = Field(
        description="Path to the file in the repository (e.g., 'config/deployment.yaml', 'src/app.py')"
    )
    edits: list[FixEdit] = Field(
        min_length=1,
        description=(
            "One or more anchored search-and-replace edits applied sequentially. "
            "Each edit's old_string must match the file exactly once "
            "(unless replace_all=true). Edits operate on the result of prior edits."
        ),
    )
    fix_description: str = Field(
        description="Human-readable description of what this fix does."
    )
    root_cause_summary: str = Field(
        description="Summary of why this change is needed - what root cause it addresses."
    )
    commit_message: Optional[str] = Field(
        default=None,
        description="Suggested commit message. If not provided, one is generated."
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repository in 'owner/repo' format. Required when multiple repos are connected."
    )
    branch: Optional[str] = Field(
        default=None,
        description="Target branch for the fix. Defaults to the repository's default branch."
    )


# ---------------------------------------------------------------------------
# Line-ending helpers
# ---------------------------------------------------------------------------


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _restore_line_ending(text: str, ending: str) -> str:
    return text if ending == "\n" else text.replace("\n", "\r\n")


def _has_mixed_line_endings(text: str) -> bool:
    """True iff `text` contains both CRLF and standalone LF.

    Normalizing a mixed-ending file to LF and restoring as CRLF (or vice versa)
    would rewrite every line terminator and produce a giant diff for what
    should be a tiny edit — so callers should refuse mixed input instead.
    """
    if "\r\n" not in text:
        return False
    return "\n" in text.replace("\r\n", "")


# ---------------------------------------------------------------------------
# Levenshtein (for BlockAnchorReplacer similarity scoring)
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    curr = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        curr[0] = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return prev[len(b)]


# ---------------------------------------------------------------------------
# Replacers — each yields candidate substrings of `content` that should be
# treated as a match for `find`. The driver picks the unique match (or all
# matches, for replace_all) and performs the actual replacement.
# ---------------------------------------------------------------------------


Replacer = Callable[[str, str], Generator[str, None, None]]


def _simple_replacer(content: str, find: str) -> Generator[str, None, None]:
    if find and find in content:
        yield find


def _line_trimmed_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Match per-line modulo leading/trailing whitespace per line."""
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if not search_lines:
        return

    for i in range(len(original_lines) - len(search_lines) + 1):
        ok = True
        for j, sline in enumerate(search_lines):
            if original_lines[i + j].strip() != sline.strip():
                ok = False
                break
        if not ok:
            continue
        # Reconstruct exact substring of `content` that spans these lines
        start = sum(len(original_lines[k]) + 1 for k in range(i))
        end = start
        for k in range(len(search_lines)):
            end += len(original_lines[i + k])
            if k < len(search_lines) - 1:
                end += 1  # newline
        yield content[start:end]


# Match opencode's battle-tested thresholds. Anchors plus the per-edit length
# check (above) keep these from accepting wildly-different middle content.
_SINGLE_CANDIDATE_SIMILARITY = 0.0
_MULTIPLE_CANDIDATES_SIMILARITY = 0.3


def _block_anchor_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Match blocks whose first and last (trimmed) lines anchor, with middle
    judged by average Levenshtein similarity. Requires ≥3 lines. To avoid
    swallowing extra lines when the candidate block is much larger than the
    search block, candidates with very different line counts are skipped."""
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if len(search_lines) < 3:
        return

    first = search_lines[0].strip()
    last = search_lines[-1].strip()
    search_size = len(search_lines)

    candidates: list[tuple[int, int]] = []
    for i, line in enumerate(original_lines):
        if line.strip() != first:
            continue
        # Don't break on the first matching closer — repeated closers (e.g.
        # multiple `}` after one opener) mean the correct end may be further
        # out. Collect every (open, close) pair and let _similarity rank them.
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last:
                candidates.append((i, j))

    if not candidates:
        return

    def _emit(start_line: int, end_line: int) -> str:
        start = sum(len(original_lines[k]) + 1 for k in range(start_line))
        end = start
        for k in range(start_line, end_line + 1):
            end += len(original_lines[k])
            if k < end_line:
                end += 1
        return content[start:end]

    def _similarity(start_line: int, end_line: int) -> float:
        """Average per-line similarity across ALL middle lines on BOTH sides.
        Missing lines (when sizes differ) score as 0 so a too-long candidate
        can't get a free pass on its untested tail."""
        actual_size = end_line - start_line + 1
        s_middle = max(search_size - 2, 0)
        a_middle = max(actual_size - 2, 0)
        if s_middle == 0 and a_middle == 0:
            return 1.0
        denom = max(s_middle, a_middle)
        score = 0.0
        for k in range(1, denom + 1):
            orig = original_lines[start_line + k].strip() if k < actual_size - 1 else ""
            srch = search_lines[k].strip() if k < search_size - 1 else ""
            max_len = max(len(orig), len(srch))
            if max_len == 0:
                # Both lines empty → exact match
                score += 1.0
                continue
            score += 1 - _levenshtein(orig, srch) / max_len
        return score / denom

    if len(candidates) == 1:
        s, e = candidates[0]
        if _similarity(s, e) >= _SINGLE_CANDIDATE_SIMILARITY:
            yield _emit(s, e)
        return

    best: Optional[tuple[int, int]] = None
    best_score = -1.0
    for s, e in candidates:
        sc = _similarity(s, e)
        if sc > best_score:
            best_score = sc
            best = (s, e)
    if best is not None and best_score >= _MULTIPLE_CANDIDATES_SIMILARITY:
        yield _emit(*best)


def _whitespace_normalized_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Match where all runs of whitespace collapse to a single space."""
    import re

    def normalize(t: str) -> str:
        return re.sub(r"\s+", " ", t).strip()

    norm_find = normalize(find)
    if not norm_find:
        return

    lines = content.split("\n")
    for line in lines:
        if normalize(line) == norm_find:
            yield line
        else:
            if norm_find in normalize(line):
                words = find.strip().split()
                if words:
                    pattern = r"\s+".join(re.escape(w) for w in words)
                    try:
                        m = re.search(pattern, line)
                        if m:
                            yield m.group(0)
                    except re.error as exc:
                        logger.debug("Skipping invalid regex pattern in whitespace-normalized replacer: %s", exc)

    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i:i + len(find_lines)])
            if normalize(block) == norm_find:
                yield block


def _indentation_flexible_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Strip common minimum indentation from both sides and compare."""
    def strip_min_indent(t: str) -> str:
        ls = t.split("\n")
        non_empty = [l for l in ls if l.strip()]
        if not non_empty:
            return t
        min_indent = min(len(l) - len(l.lstrip(" \t")) for l in non_empty)
        return "\n".join(l if not l.strip() else l[min_indent:] for l in ls)

    norm_find = strip_min_indent(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i:i + len(find_lines)])
        if strip_min_indent(block) == norm_find:
            yield block


def _unescape_string(s: str) -> str:
    """Convert backslash-escape sequences (\\n, \\t, \\", \\\\, ...) to their literal forms."""
    import re
    return re.sub(
        r'\\(n|t|r|\'|"|`|\\|\n|\$)',
        lambda m: {
            "n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"',
            "`": "`", "\\": "\\", "\n": "\n", "$": "$",
        }.get(m.group(1), m.group(0)),
        s,
    )


def _escape_normalized_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Treat backslash-escapes (\\n, \\t, \\\", etc.) in find as their literal.
    Skipped when find contains no escape sequences (would duplicate _simple_replacer)."""
    unesc_find = _unescape_string(find)
    if unesc_find == find:
        # No escapes present — nothing this replacer adds over _simple_replacer.
        return
    if unesc_find and unesc_find in content:
        yield unesc_find

    lines = content.split("\n")
    find_lines = unesc_find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i:i + len(find_lines)])
        if _unescape_string(block) == unesc_find:
            yield block


def _trimmed_boundary_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Try matching with leading/trailing whitespace stripped from find."""
    trimmed = find.strip()
    if trimmed == find:
        return
    if trimmed and trimmed in content:
        yield trimmed
    lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i:i + len(find_lines)])
        if block.strip() == trimmed:
            yield block


def _context_aware_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Block-anchor variant requiring same line count and ≥50% trimmed-line match."""
    find_lines = find.split("\n")
    if find_lines and find_lines[-1] == "":
        find_lines.pop()
    if len(find_lines) < 3:
        return

    content_lines = content.split("\n")
    first = find_lines[0].strip()
    last = find_lines[-1].strip()

    for i, line in enumerate(content_lines):
        if line.strip() != first:
            continue
        # Scan every matching closer; a closer further out may be the right
        # one when there are repeated trailing anchors. The driver dedupes
        # identical yields and detects multi-match ambiguity.
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() != last:
                continue
            block_lines = content_lines[i:j + 1]
            if len(block_lines) != len(find_lines):
                continue
            matching = 0
            total = 0
            for k in range(1, len(block_lines) - 1):
                bl = block_lines[k].strip()
                fl = find_lines[k].strip()
                if bl or fl:
                    total += 1
                    if bl == fl:
                        matching += 1
            if total == 0 or matching / total >= 0.5:
                yield "\n".join(block_lines)


def _multi_occurrence_replacer(content: str, find: str) -> Generator[str, None, None]:
    """Yield every exact occurrence of find. Used with replace_all."""
    if not find:
        return
    start = 0
    while True:
        idx = content.find(find, start)
        if idx == -1:
            break
        yield find
        start = idx + len(find)


_REPLACERS: list[tuple[str, Replacer]] = [
    ("simple", _simple_replacer),
    ("line_trimmed", _line_trimmed_replacer),
    ("block_anchor", _block_anchor_replacer),
    ("whitespace_normalized", _whitespace_normalized_replacer),
    ("indentation_flexible", _indentation_flexible_replacer),
    ("escape_normalized", _escape_normalized_replacer),
    ("trimmed_boundary", _trimmed_boundary_replacer),
    ("context_aware", _context_aware_replacer),
    ("multi_occurrence", _multi_occurrence_replacer),
]

# Only these replacers preserve byte-equality with old_string, so str.replace
# can safely apply the same edit to every occurrence under replace_all.
_REPLACE_ALL_SAFE = {"simple", "multi_occurrence"}


def _find_closest_lines(content: str, find: str, max_results: int = 3) -> list[tuple[int, str]]:
    """Return up to ``max_results`` (1-based line_no, line) pairs from ``content``
    most similar to the most distinctive (longest non-empty) line of ``find``.
    Used to give the LLM concrete "did you mean this line?" hints."""
    find_lines = [l for l in find.split("\n") if l.strip()]
    if not find_lines:
        return []
    anchor = max(find_lines, key=len).strip()
    if not anchor:
        return []
    scored: list[tuple[float, int, str]] = []
    for i, line in enumerate(content.split("\n")):
        stripped = line.strip()
        if not stripped:
            continue
        max_len = max(len(stripped), len(anchor))
        sim = 1.0 - _levenshtein(stripped, anchor) / max_len
        if sim > 0.3:
            scored.append((sim, i + 1, line))
    scored.sort(key=lambda t: -t[0])
    return [(ln, line) for _sim, ln, line in scored[:max_results]]


def _find_all_match_lines(content: str, needle: str, max_results: int = 10) -> list[int]:
    """Return up to ``max_results`` distinct 1-based line numbers where
    ``needle`` appears in ``content``. Multiple matches on the same line
    collapse to one entry so the LLM doesn't see ``L5, L5, L5``."""
    if not needle:
        return []
    seen: list[int] = []
    start = 0
    while len(seen) < max_results:
        idx = content.find(needle, start)
        if idx == -1:
            break
        ln = content.count("\n", 0, idx) + 1
        if ln not in seen:
            seen.append(ln)
        start = idx + max(len(needle), 1)
    return seen


def _locate_with_chain(
    content: str, old: str, new: str, replace_all: bool
) -> tuple[list[tuple[int, int, str]], Optional[str]]:
    """Find match spans for `old` in `content` via the replacer chain.

    Returns (list of (start, end, replacement_text), error). Pure-locate
    function; does NOT modify content. Callers apply spans atomically so
    that a multi-edit batch can't have later edits accidentally match text
    inserted by earlier ones — every edit anchors to the ORIGINAL file.
    """
    if old == new:
        return [], "old_string and new_string are identical (no-op)"
    if not old:
        return [], "old_string is empty"

    found_any = False
    fuzzy_only = False
    multi_match_candidate: Optional[str] = None
    for name, replacer in _REPLACERS:
        for candidate in replacer(content, old):
            idx = content.find(candidate)
            if idx == -1:
                continue
            found_any = True

            if replace_all:
                if name not in _REPLACE_ALL_SAFE:
                    fuzzy_only = True
                    continue
                spans: list[tuple[int, int, str]] = []
                pos = 0
                while True:
                    p = content.find(candidate, pos)
                    if p == -1:
                        break
                    spans.append((p, p + len(candidate), new))
                    pos = p + len(candidate)
                return spans, None

            last_idx = content.rfind(candidate)
            if idx != last_idx:
                if multi_match_candidate is None:
                    multi_match_candidate = candidate
                continue

            return [(idx, idx + len(candidate), new)], None

    if not found_any:
        hints = _find_closest_lines(content, old)
        if hints:
            hint_block = "\nClosest lines in the file (copy verbatim — keep quotes, whitespace, indent):\n"
            for ln, line in hints:
                hint_block += f"  L{ln}: {line!r}\n"
            hint_block += (
                "Pick the right one and include 1-3 surrounding lines as anchor context so "
                "the match is unique. Then re-call github_fix."
            )
        else:
            hint_block = (
                " Call get_file_contents for this file first, then copy old_string byte-for-byte "
                "from the response and retry."
            )
        return [], (
            "old_string not found in the current file (tried exact, line-trimmed, "
            "block-anchor, whitespace-normalized, indentation-flexible, escape-"
            "normalized, trimmed-boundary, and context-aware matching)." + hint_block
        )
    if replace_all and fuzzy_only:
        return [], (
            "replace_all=true requires old_string to match the file exactly. "
            "Fix the whitespace/indentation in old_string to match the file, "
            "or split into individual edits with replace_all=false."
        )
    match_lines = _find_all_match_lines(content, multi_match_candidate or old)
    locs = ", ".join(f"L{n}" for n in match_lines) if match_lines else "multiple places"
    return [], (
        f"old_string matches {len(match_lines) or 'multiple'} places in the file "
        f"({locs}). Extend old_string with 1-3 unique surrounding lines as anchor "
        "context (e.g. the function header above, the closing brace below) and "
        "retry. Use replace_all=true only if you want every occurrence replaced."
    )


# An anchored edit should target a small window around the change, not the
# whole file. If old_string covers more than this fraction of the original,
# the LLM is doing a whole-file rewrite under the guise of an edit — refuse.
_MAX_OLD_STRING_RATIO = 0.5
_OLD_STRING_RATIO_MIN_FILE = 500  # don't enforce on tiny files


def _apply_edits(original: str, edits: list) -> tuple[Optional[str], Optional[str]]:
    """Apply edits atomically against the original buffer.

    Each edit is located independently in the ORIGINAL content (not the
    running content), so edit N can never silently match text that edit
    N-1 inserted. Overlapping spans across edits are rejected.
    """
    if _has_mixed_line_endings(original):
        return None, (
            "file has mixed CRLF and LF line endings — refusing to apply edits "
            "because normalizing the buffer would rewrite every line terminator "
            "and produce a huge diff. Normalize the file's line endings first."
        )
    line_ending = _detect_line_ending(original)
    content = _normalize_lf(original)
    orig_lf_len = len(content)

    all_spans: list[tuple[int, int, str]] = []
    span_owner: dict[int, int] = {}  # span start → edit index, for overlap errors

    for i, raw in enumerate(edits, 1):
        if isinstance(raw, dict):
            old = raw.get("old_string", "")
            new = raw.get("new_string", "")
            replace_all = bool(raw.get("replace_all", False))
        else:
            old = getattr(raw, "old_string", "")
            new = getattr(raw, "new_string", "")
            replace_all = bool(getattr(raw, "replace_all", False))

        old_lf = _normalize_lf(old)
        new_lf = _normalize_lf(new)

        if orig_lf_len > _OLD_STRING_RATIO_MIN_FILE and len(old_lf) > _MAX_OLD_STRING_RATIO * orig_lf_len:
            return None, (
                f"edit {i}: old_string is {len(old_lf)/orig_lf_len:.0%} of the "
                f"file ({len(old_lf)} of {orig_lf_len} chars). An anchored edit "
                "should target a narrow window around the change - usually 1-3 "
                "lines of context above and below. Break this into smaller "
                "targeted edits instead of regenerating the whole file."
            )

        spans, err = _locate_with_chain(content, old_lf, new_lf, replace_all)
        if err is not None:
            return None, f"edit {i}: {err}"

        for s, e, replacement in spans:
            for ps, pe, _existing in all_spans:
                if s < pe and e > ps:
                    return None, (
                        f"edit {i}: target region [{s}:{e}] overlaps a region "
                        f"already claimed by edit {span_owner.get(ps, '?')}. "
                        "Each edit must anchor to a distinct slice of the "
                        "original file — combine them into one edit or rework "
                        "the anchors."
                    )
            span_owner[s] = i
            all_spans.append((s, e, replacement))

    all_spans.sort(key=lambda t: t[0])
    parts: list[str] = []
    cursor = 0
    for s, e, replacement in all_spans:
        parts.append(content[cursor:s])
        parts.append(replacement)
        cursor = e
    parts.append(content[cursor:])

    return _restore_line_ending("".join(parts), line_ending), None


# ---------------------------------------------------------------------------
# GitHub + DB plumbing (unchanged from prior version)
# ---------------------------------------------------------------------------


def _resolve_repository(
    user_id: str,
    explicit_repo: Optional[str] = None
) -> tuple[Optional[str], Optional[str], str]:
    from .github_rca_tool import _resolve_repository as rca_resolve_repo
    return rca_resolve_repo(user_id, explicit_repo)


def _get_file_content(owner: str, repo: str, path: str, branch: Optional[str], user_id: str) -> Optional[str]:
    args = {"owner": owner, "repo": repo, "path": path}
    if branch:
        args["ref"] = f"refs/heads/{branch}"
    result = call_github_mcp_sync("get_file_contents", args, user_id)
    return parse_file_content_response(result)


def _save_fix_suggestion(
    incident_id: str,
    user_id: str,
    title: str,
    description: str,
    file_path: str,
    original_content: Optional[str],
    suggested_content: str,
    repository: str,
    commit_message: Optional[str],
) -> Optional[int]:
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO incident_suggestions
                (incident_id, title, description, type, risk, file_path,
                 original_content, suggested_content, repository, command)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    incident_id,
                    title,
                    description,
                    "fix",
                    "medium",
                    file_path,
                    original_content,
                    suggested_content,
                    repository,
                    commit_message,
                )
            )
            result = cursor.fetchone()
            conn.commit()
            suggestion_id = result[0] if result else None
            if suggestion_id:
                logger.info(f"Saved fix suggestion {suggestion_id} for incident {incident_id}")
            return suggestion_id
    except Exception as e:
        logger.error(f"Failed to save fix suggestion: {e}", exc_info=True)
        return None


def _build_title(file_path: str, fix_description: str) -> str:
    filename = file_path.split('/')[-1]
    truncated_desc = fix_description[:50]
    suffix = "..." if len(fix_description) > 50 else ""
    return f"Fix {filename}: {truncated_desc}{suffix}"


def github_fix(
    file_path: str,
    edits: list,
    fix_description: str,
    root_cause_summary: str,
    commit_message: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Suggest a code fix via anchored multi-edit applied server-side."""
    if not user_id:
        return build_error_response("User ID is required")
    if not incident_id:
        return build_error_response("Incident ID is required. This tool should be used during RCA.")
    if not edits:
        return build_error_response("edits must contain at least one entry")

    owner, repo_name, source = _resolve_repository(user_id, repo)
    if not owner or not repo_name:
        return build_error_response(
            "Could not resolve repository. Please specify repo='owner/repo' or add repo info to Knowledge Base."
        )

    full_repo = f"{owner}/{repo_name}"
    logger.info(f"[github_fix] Using repository {full_repo} (resolved from {source})")

    original_content = _get_file_content(owner, repo_name, file_path, branch, user_id)
    if original_content is None:
        return build_error_response(
            f"Could not fetch current contents of {file_path} from {full_repo}. "
            "Verify the path and branch, then retry."
        )

    suggested_content, apply_err = _apply_edits(original_content, edits)
    if apply_err or suggested_content is None:
        logger.warning("[github_fix] edit application failed for %s: %s", file_path, apply_err)
        return build_error_response(apply_err or "edit application failed")

    if suggested_content == original_content:
        return build_error_response(
            "Applied edits produced no change to the file. Double-check old_string/new_string."
        )
    if not suggested_content.strip():
        # An edit that empties the entire file is almost always a mistake and
        # push_files would silently truncate the file. Reject explicitly.
        return build_error_response(
            "Applied edits produced an empty (or whitespace-only) file. If you "
            "really intend to empty this file, do it manually — github_fix is "
            "for targeted code changes."
        )

    final_commit_message = commit_message or f"fix: {fix_description[:100]}"
    title = _build_title(file_path, fix_description)
    description = f"{fix_description}\n\n**Root Cause:** {root_cause_summary}"

    suggestion_id = _save_fix_suggestion(
        incident_id=incident_id,
        user_id=user_id,
        title=title,
        description=description,
        file_path=file_path,
        original_content=original_content,
        suggested_content=suggested_content,
        repository=full_repo,
        commit_message=final_commit_message,
    )

    if not suggestion_id:
        return build_error_response("Failed to save fix suggestion to database")

    return build_success_response(
        message="Fix suggestion saved for user review",
        suggestion_id=suggestion_id,
        repository=full_repo,
        file_path=file_path,
        edits_applied=len(edits),
        next_steps="The user can review and edit the suggested fix in the Incidents UI, then create a PR when ready."
    )

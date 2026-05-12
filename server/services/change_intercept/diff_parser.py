"""Unified-diff parser used by the verdict validator (Part 2).

The investigator emits findings tagged with ``(file_path, start_line,
end_line)``. The validator must verify that those line numbers actually
correspond to a hunk in the staged diff — otherwise the LLM has
hallucinated a finding. This module provides the parsing + lookup
helpers that the validator calls.

Phase 1a scope: handle the unified-diff format GitHub returns via
``Accept: application/vnd.github.diff``. The parser is intentionally
tolerant of GitHub-specific noise lines (``\\ No newline at end of file``,
``index abc..def`` headers, ``similarity index``, ``rename from / to``,
``Binary files differ``) and bails gracefully on malformed input rather
than raising — a parse failure becomes ``DiffIndex(files={})``, which
the validator treats as "drop every finding" (under-block bias).

This module has zero external dependencies and is unit-testable in
isolation from any HTTP / DB code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Hunk header: ``@@ -<old_start>,<old_count> +<new_start>,<new_count> @@ [context]``.
# Counts default to 1 when omitted (``@@ -10 +10 @@``).
_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)

# File header preamble lines from GitHub's diff format.
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_FROM_FILE_RE = re.compile(r"^---\s+(.+)$")
_TO_FILE_RE = re.compile(r"^\+\+\+\s+(.+)$")


@dataclass(frozen=True)
class DiffHunk:
    """A single hunk in a unified diff.

    Attributes:
        new_start: first new-file line covered by this hunk (1-indexed).
        new_count: how many new-file lines this hunk covers (≥1).
        added_lines: set of new-file line numbers that this hunk
            actually added or modified (i.e. lines that begin with
            ``+`` in the diff, excluding the ``+++`` header).

    The distinction between ``new_start/new_count`` (the whole hunk
    range) and ``added_lines`` (the actually-changed subset) matters
    for finding validation: a strict anchor requires the line to be
    in ``added_lines``; a lax anchor lets findings sit on adjacent
    context lines within the hunk range. The validator uses the
    strict check by default.
    """

    new_start: int
    new_count: int
    added_lines: frozenset[int]


@dataclass
class DiffIndex:
    """Parsed-diff lookup index keyed by post-change file path.

    Attributes:
        files: ``{file_path: [DiffHunk, ...]}``. A file with an empty
            hunk list means the file was renamed without content
            changes or was a binary diff — the validator treats both
            as "drop findings against this file" since there are no
            lines to anchor to.

    The class is intentionally non-frozen so the parser can mutate it
    incrementally as it walks the input; the lookup methods are
    side-effect free.
    """

    files: dict[str, list[DiffHunk]] = field(default_factory=dict)

    def is_changed_line(self, file_path: str, line: int) -> bool:
        """Return ``True`` iff ``line`` was added/modified in
        ``file_path``.

        Strict anchor — used by the validator to enforce that every
        finding points at a line the engineer actually changed.
        """
        for hunk in self.files.get(file_path, ()):
            if line in hunk.added_lines:
                return True
        return False

    def is_in_hunk(self, file_path: str, line: int) -> bool:
        """Return ``True`` iff ``line`` falls inside any hunk's range
        for ``file_path``, even on a context line.

        Lax anchor — used as a secondary check when the strict anchor
        fails but the finding range overlaps a hunk we touched. The
        validator currently uses the strict check; this is provided
        for future use when we relax for multi-line findings.
        """
        for hunk in self.files.get(file_path, ()):
            if hunk.new_start <= line < hunk.new_start + max(hunk.new_count, 1):
                return True
        return False

    def files_changed(self) -> tuple[str, ...]:
        """Return the tuple of file paths the diff touched.

        Empty tuple for an empty / unparseable diff. The investigator's
        prompt builder uses this for the "files changed" summary it
        renders alongside the unified diff.
        """
        return tuple(sorted(self.files.keys()))


def parse_unified_diff(diff_text: str) -> DiffIndex:
    """Parse a unified diff into a ``DiffIndex``.

    Tolerant of GitHub-specific preamble lines and bails gracefully
    on malformed input — a parse failure returns an empty index, not
    an exception. Empty / whitespace-only input is also an empty
    index (the dispatcher persists snapshots for PRs that may not
    have any diff content yet).

    Args:
        diff_text: full unified diff as returned by GitHub's
            ``Accept: application/vnd.github.diff`` endpoint.

    Returns:
        A ``DiffIndex`` populated with one entry per file the diff
        touched. Binary diffs and pure-rename diffs land with an
        empty hunk list.
    """
    index = DiffIndex()
    if not diff_text or not diff_text.strip():
        return index

    current_path: str | None = None
    # Track whether the previous ``+++`` header pointed at /dev/null
    # (file deletion) — we skip those since there's nothing to anchor.
    deletion_in_progress: bool = False
    # Per-hunk state. ``hunk_new_cursor`` is the next line-number we'd
    # assign to a context (' ') or added ('+') line as we walk the body.
    current_hunk_new_start: int = 0
    current_hunk_new_count: int = 0
    current_hunk_added: set[int] = set()
    hunk_new_cursor: int = 0
    in_hunk: bool = False

    def _flush_hunk() -> None:
        """Persist the current hunk (if any) to the index."""
        nonlocal current_hunk_added, in_hunk
        if not in_hunk or current_path is None:
            return
        if deletion_in_progress:
            current_hunk_added = set()
            in_hunk = False
            return
        index.files.setdefault(current_path, []).append(
            DiffHunk(
                new_start=current_hunk_new_start,
                new_count=current_hunk_new_count,
                added_lines=frozenset(current_hunk_added),
            )
        )
        current_hunk_added = set()
        in_hunk = False

    for raw_line in diff_text.splitlines():
        line = raw_line  # don't strip — leading whitespace is meaningful

        # File-boundary detection. Reset per-file state on each
        # ``diff --git`` header.
        if line.startswith("diff --git"):
            _flush_hunk()
            m = _DIFF_GIT_RE.match(line)
            current_path = m.group(2) if m else None
            deletion_in_progress = False
            in_hunk = False
            # Ensure the file appears in the index even if it ends up
            # with no hunks (rename-only, binary). The validator can
            # then explicitly drop findings against renamed-only files
            # rather than silently treat them as "unknown file."
            if current_path:
                index.files.setdefault(current_path, [])
            continue

        # GitHub's diff includes ``index <sha>..<sha> <mode>`` between
        # the ``diff --git`` and the ``--- / +++`` headers. Skip.
        if line.startswith("index ") or line.startswith("similarity index"):
            continue

        if line.startswith("rename from") or line.startswith("rename to"):
            # Rename-without-content-change has no hunks; we already
            # captured the new path from the ``diff --git`` line.
            continue

        if line.startswith("Binary files"):
            # No hunks for binaries; keep the file in the index with
            # an empty list so the validator can distinguish "unknown"
            # from "binary."
            continue

        # ``---`` (old file) and ``+++`` (new file) headers carry the
        # ``a/`` and ``b/`` prefixed paths. We prefer the ``b/`` path
        # (post-change) as the canonical file path; falling back to
        # the ``diff --git`` line we already captured if these are
        # missing.
        if line.startswith("--- "):
            m = _FROM_FILE_RE.match(line)
            if m and m.group(1).strip() == "/dev/null":
                # File is being created — we'll keep the ``+++`` path.
                deletion_in_progress = False
            continue

        if line.startswith("+++ "):
            m = _TO_FILE_RE.match(line)
            if m:
                path_token = m.group(1).strip()
                if path_token == "/dev/null":
                    deletion_in_progress = True
                else:
                    # Strip the leading ``b/`` GitHub adds.
                    if path_token.startswith("b/"):
                        path_token = path_token[2:]
                    current_path = path_token
                    deletion_in_progress = False
                    index.files.setdefault(current_path, [])
            continue

        # Hunk header. Closes any prior open hunk for this file.
        if line.startswith("@@"):
            _flush_hunk()
            m = _HUNK_HEADER_RE.match(line)
            if not m or current_path is None or deletion_in_progress:
                continue
            current_hunk_new_start = int(m.group(3))
            current_hunk_new_count = int(m.group(4) or "1")
            current_hunk_added = set()
            hunk_new_cursor = current_hunk_new_start
            in_hunk = True
            continue

        if not in_hunk:
            # Pre-hunk noise (commit messages on combined diffs,
            # ``\\ No newline at end of file`` outside a hunk). Skip.
            continue

        # Inside a hunk body:
        #   '+' → added line in the new file at hunk_new_cursor
        #   '-' → removed line, new-file cursor doesn't advance
        #   ' ' (or '') → context line, advances new-file cursor
        #   '\\' → "\ No newline at end of file" marker, skip
        if line.startswith("+"):
            current_hunk_added.add(hunk_new_cursor)
            hunk_new_cursor += 1
            continue

        if line.startswith("-"):
            continue

        if line.startswith("\\"):
            # No-newline marker. Don't advance the cursor.
            continue

        # Context line (either explicit ' ' prefix or empty line inside
        # a hunk — GitHub trims trailing whitespace so '' is valid for
        # an empty context line).
        hunk_new_cursor += 1

    # Flush the trailing hunk after the last line.
    _flush_hunk()
    return index

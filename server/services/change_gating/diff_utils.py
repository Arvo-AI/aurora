"""Unified-diff utilities for PR change gating.

Pure functions: parse RIGHT-side commentable line numbers out of a
unified diff, split agent findings into anchorable vs unanchorable
(GitHub 422s on inline comments outside diff hunks), and bound the
diff text included in the agent prompt.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# "@@ -a,b +c,d @@ optional section" — b and d default to 1 when omitted.
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Total budget for the per-file diff block in the prompt, and the per-file cap
# that stops one huge file from crowding out every other changed file.
DEFAULT_MAX_DIFF_CHARS = 60_000
DEFAULT_MAX_FILE_DIFF_CHARS = 15_000


def parse_diff_hunks(
    diff_text: Optional[str], added_only: bool = False
) -> Dict[str, Set[int]]:
    """Map file path -> set of RIGHT-side line numbers visible in diff hunks.

    Both context (`` ``) and added (``+``) lines are commentable on
    GitHub's RIGHT side; ``-`` lines exist only on the left and do not
    advance the right-side counter. Files deleted entirely
    (``+++ /dev/null``) have no right side and are skipped.

    When ``added_only`` is True, only ADDED (``+``) lines are recorded —
    context lines advance the counter but are excluded. Incremental reviews
    use this so a finding the agent raised on an unchanged context line of
    the compare diff (pre-existing code already reviewed) is NOT mistaken
    for a risk in the new commits.

    Hunk content is consumed by the ``-a,b +c,d`` line counts BEFORE any
    header detection runs, so added/removed lines whose content begins
    with ``++ `` or ``-- `` (rendering as ``+++ ``/``--- ``) are never
    misparsed as file headers mid-hunk.
    """
    hunks: Dict[str, Set[int]] = {}
    current_file: Optional[str] = None
    right_line = 0
    left_remaining = 0  # left-side lines unconsumed in the current hunk
    right_remaining = 0  # right-side lines unconsumed in the current hunk

    for line in (diff_text or "").splitlines():
        if left_remaining > 0 or right_remaining > 0:
            # Inside a hunk: every line belongs to the hunk until both
            # side counters are exhausted — regardless of its prefix.
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" — not a real line
            if line.startswith("-"):
                left_remaining -= 1
                continue  # left-side only; right counter does not advance
            is_added = line.startswith("+")
            if is_added:
                right_remaining -= 1
            else:
                # Context line (" " prefixed, or bare "" from some generators).
                left_remaining -= 1
                right_remaining -= 1
            if current_file is not None and (is_added or not added_only):
                hunks[current_file].add(right_line)
            right_line += 1
            continue

        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target == "/dev/null":
                current_file = None
            else:
                current_file = target[2:] if target.startswith("b/") else target
                hunks.setdefault(current_file, set())
        elif line.startswith("@@"):
            match = _HUNK_HEADER_RE.match(line)
            if match:
                left_remaining = int(match.group(1)) if match.group(1) is not None else 1
                right_line = int(match.group(2))
                right_remaining = int(match.group(3)) if match.group(3) is not None else 1
        # Anything else between hunks/files (diff --git, index, --- lines)
        # is ignored.

    return hunks


def anchor_findings(
    findings: List[Dict[str, Any]], hunks: Dict[str, Set[int]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split findings into (anchored, unanchored).

    A finding anchors iff its ``file_path`` is in ``hunks`` AND its
    ``line`` is an int present in that file's right-side line set.
    Findings with a missing/None line are unanchored. This is the guard
    against GitHub's 422 on inline comments outside diff hunks.
    """
    anchored: List[Dict[str, Any]] = []
    unanchored: List[Dict[str, Any]] = []
    for finding in findings or []:
        file_path = finding.get("file_path")
        line = finding.get("line")
        if (
            isinstance(line, int)
            # bool is a subclass of int in Python — exclude True/False lines
            and not isinstance(line, bool)
            and file_path in hunks
            and line in hunks[file_path]
        ):
            anchored.append(finding)
        else:
            unanchored.append(finding)
    return anchored, unanchored


def format_changed_files(files: List[Dict[str, Any]]) -> List[str]:
    """Render GitHub ``list_files`` dicts as one summary line per file.

    Shared between the prompt's CHANGED FILES block and the oversized-diff
    fallback so the two can never drift.
    """
    return [
        "- {filename} ({status}, +{additions}/-{deletions})".format(
            filename=f.get("filename", "<unknown>"),
            status=f.get("status", "modified"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
        )
        for f in files or []
    ]


def _build_file_block(
    f: Dict[str, Any],
    esc: Callable[[str], str],
    max_file_chars: int,
) -> str:
    """Render one file's labelled diff section: ``### path (status, +a/-d)``
    followed by its fenced patch.

    ``esc`` defangs author-controlled text (the filename — which lands in the
    header and truncation note outside the fence — and the patch body); the
    header text, note, and ```` ```diff ```` fences stay trusted. Files GitHub
    served without a patch (binary, too large, or rename-only) get a notice
    pointing at the agent's PR-reading tools instead of a diff.
    """
    filename = esc(f.get("filename", "<unknown>"))
    header = "### {} ({}, +{}/-{})".format(
        filename,
        f.get("status", "modified"),
        f.get("additions", 0),
        f.get("deletions", 0),
    )
    patch = f.get("patch")
    if not patch:
        return (
            f"{header}\n[No inline diff served by GitHub for this file "
            "(binary, too large, or rename-only). Read its changes with "
            "your GitHub PR-reading tools if it looks risky.]"
        )
    note = ""
    if len(patch) > max_file_chars:
        patch = patch[:max_file_chars]
        note = (
            f"\n[Diff for {filename} truncated at {max_file_chars:,} chars; "
            "read the full file with your GitHub PR-reading tools if needed.]"
        )
    return f"{header}\n```diff\n{esc(patch)}\n```{note}"


def build_per_file_diff(
    files: List[Dict[str, Any]],
    diff: Optional[str] = None,
    max_total_chars: int = DEFAULT_MAX_DIFF_CHARS,
    max_file_chars: int = DEFAULT_MAX_FILE_DIFF_CHARS,
    escape: Optional[Callable[[str], str]] = None,
) -> str:
    """Render the changed files as one labelled diff section per file.

    Presents the diff file-by-file (each file's ``patch`` under a
    ``### path (status, +adds/-dels)`` heading) instead of one
    undifferentiated blob, so the review agent attends to each file in turn
    rather than skimming a single giant diff.

    ``files`` are GitHub ``list_files`` / compare ``files`` dicts; GitHub
    serves a per-file unified ``patch`` for each (omitted only for binary,
    over-limit, or rename-only files). Because those per-file patches survive
    even when the whole-PR diff media type 406s, an oversized PR degrades
    gracefully here instead of collapsing to a filename-only summary.

    A per-file cap (``max_file_chars``) keeps one huge file from crowding out
    the rest; a total cap (``max_total_chars``) bounds the whole block — every
    section (patch, no-patch notice, omitted footer) counts against it. Files
    without a servable patch — and any trimmed by the budget — are flagged so
    the agent knows to read them with its GitHub PR-reading tools if they look
    risky (no dependency on any one tool). When NO file carries a per-file
    patch but GitHub served the whole-PR ``diff``, that diff is included
    (budget-bounded) so the agent still sees real content.

    ``escape`` is applied to every piece of author-controlled text that reaches
    the prompt — the per-file patch AND the filename (which appears in the
    section header, truncation note, and omitted footer) — never to the trusted
    scaffolding, so prompt-injection defanging cannot break the section fences.
    """
    esc = escape or (lambda s: s)
    file_list = files or []

    def _fenced_raw_diff(budget: int) -> str:
        """Whole-PR diff, escaped + fenced + capped — the no-per-file fallback."""
        return "```diff\n" + esc((diff or "")[:budget]) + "\n```"

    if not file_list:
        # No file-level data at all (rare). Fall back to the raw diff if any.
        if diff:
            return _fenced_raw_diff(max_total_chars)
        return "[No file-level changes available to review.]"

    sections: List[str] = []
    omitted: List[str] = []
    total = 0
    for f in file_list:
        # filename is author-controlled (a PR can add/rename a file to any
        # name) and lands outside the ```diff fence, so it must be defanged too.
        filename = esc(f.get("filename", "<unknown>"))
        block = _build_file_block(f, esc, max_file_chars)
        # Budget applies to every block; the first is always kept so a single
        # over-cap file still yields content.
        if sections and total + len(block) > max_total_chars:
            omitted.append(filename)
            continue
        sections.append(block)
        total += len(block)

    if omitted:
        sections.append(
            f"[{len(omitted)} further changed file(s) omitted to stay within the "
            f"diff budget: {', '.join(omitted)}. Review them with your GitHub "
            "PR-reading tools if the changes above suggest risk.]"
        )

    # All files were binary / over-limit / rename-only (no per-file patch), but
    # GitHub served the whole-PR diff — include it (bounded by remaining budget)
    # rather than handing the agent only "no inline diff" notes.
    if diff and not any(f.get("patch") for f in file_list):
        remaining = max_total_chars - total
        if remaining > 0:
            sections.append(
                "Full PR diff (no per-file patches available):\n"
                + _fenced_raw_diff(remaining)
            )

    return "\n\n".join(sections)

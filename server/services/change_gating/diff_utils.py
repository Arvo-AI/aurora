"""Unified-diff utilities for PR change gating.

Pure functions: parse RIGHT-side commentable line numbers out of a
unified diff, split agent findings into anchorable vs unanchorable
(GitHub 422s on inline comments outside diff hunks), and bound the
diff text included in the agent prompt.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

# "@@ -a,b +c,d @@ optional section" — b and d default to 1 when omitted.
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

DEFAULT_MAX_DIFF_CHARS = 60_000


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


def truncate_diff_for_prompt(
    diff: Optional[str],
    files: List[Dict[str, Any]],
    max_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> str:
    """Return the diff unchanged if small enough, else a file summary.

    ``diff=None`` (GitHub refuses the diff media type for very large PRs
    with a 406) is treated like an oversized diff. The summary is built
    from the GitHub ``list_files`` dicts and tells the agent to fetch
    targeted per-file diffs via its ``github_rca`` tool instead.
    """
    if diff is not None and len(diff) <= max_chars:
        return diff

    size_note = (
        f"The full diff is {len(diff):,} characters — too large to inline "
        f"(limit {max_chars:,})."
        if diff is not None
        else "GitHub declined to serve the full diff (the PR is too large)."
    )
    return (
        f"[{size_note} It has been replaced with the changed-file "
        "summary below. Use the github_rca tool to fetch targeted per-file "
        "diffs for the files you need to inspect.]\n\n"
        "Changed files:\n" + "\n".join(format_changed_files(files))
    )

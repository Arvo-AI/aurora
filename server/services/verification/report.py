"""Verification reports: parse what CI published, decide what it means, render it.

A repository's CI boots a pull request in a throwaway environment and uploads
one small artifact, ``aurora-verification``, holding ``verification.json``
(written by ``scripts/preview/report.py`` in this repo; the schema is documented
there). This module turns that file into something Aurora can store and show.

Everything here is pure: no network, no database, no LLM. The Celery task in
``tasks/verification.py`` does the I/O and calls in.

Trust model
-----------
The report is produced by the pull request's own code running in the pull
request's own workflow, so every field is attacker-controllable: a hostile PR
can claim any result it likes. Three consequences shape this module:

- **Nothing is taken at face value.** :func:`parse_report` rebuilds the report
  field by field with enums, counts and lengths bounded, and anything
  unrecognised dropped.
- **Text is never rendered raw.** The result is posted as a comment by Aurora's
  bot account, so a check name carrying a link, a mention or an HTML comment
  would be Aurora saying it. :func:`_text` and :func:`_code` neutralise those.
- **The reader is told what the result does not cover.** A pass only speaks for
  the files the run exercises (:func:`uncovered_paths`), and a PR that edits the
  verification itself is flagged (:func:`verification_changes`).
"""

from __future__ import annotations

import fnmatch
import io
import json
import re
import zipfile
from typing import Any, Dict, List, Optional

ARTIFACT_NAME = "aurora-verification"
REPORT_FILENAME = "verification.json"
SUPPORTED_SCHEMA = 1

# The report is a few kilobytes. These caps bound what a hostile artifact can
# make Aurora download, unpack and store.
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 256 * 1024

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"

_CHECK_STATUSES = ("pass", "warn", "fail", "skip")
_MAX_CHECKS = 200
_MAX_PATTERNS = 100
_MAX_NAME_CHARS = 120
_MAX_DETAIL_CHARS = 300
_MAX_GROUP_CHARS = 40
_MAX_PATTERN_CHARS = 200
_MAX_PATH_CHARS = 200

# Rendering caps: the comment stays readable however large the PR or report.
_MAX_FAILED_ROWS = 25
_MAX_LISTED_PATHS = 15

# Paths that define a GitHub workflow run, whatever the report declares. A PR
# that removes its own ``defined_by`` list must not escape the warning, and
# the workflow file is the root of trust for everything else in the run.
_ALWAYS_DEFINED_BY = (".github/workflows/**", ".github/actions/**")

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_PATTERN_RE = re.compile(r"^[A-Za-z0-9._\-/*?\[\]]+$")
_GROUP_RE = re.compile(r"[^a-z0-9 _-]")

_MARKER_PREFIX = "aurora-verification"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _clean(value: Any, limit: int) -> str:
    """One bounded line of text: control characters out, whitespace collapsed."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = _CONTROL_CHARS_RE.sub(" ", str(value))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_patterns(value: Any) -> List[str]:
    """Keep only well-formed path patterns; silently drop the rest."""
    if not isinstance(value, list):
        return []
    patterns: List[str] = []
    for item in value[:_MAX_PATTERNS]:
        if not isinstance(item, str):
            continue
        pattern = item.strip()
        if 0 < len(pattern) <= _MAX_PATTERN_CHARS and _PATTERN_RE.match(pattern):
            patterns.append(pattern)
    return patterns


def extract_report_bytes(artifact_zip: bytes) -> Optional[bytes]:
    """Pull ``verification.json`` out of a downloaded artifact archive.

    Reads the one member in memory and never extracts to disk, so a crafted
    member name cannot write outside a directory. The declared uncompressed
    size is checked before reading, which is what stops a zip bomb.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(artifact_zip)) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.filename.rsplit("/", 1)[-1] != REPORT_FILENAME:
                    continue
                if info.file_size > MAX_REPORT_BYTES:
                    return None
                with archive.open(info) as member:
                    data = member.read(MAX_REPORT_BYTES + 1)
                return data if len(data) <= MAX_REPORT_BYTES else None
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return None
    return None


def parse_report(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate and normalise a report. Returns ``None`` when it is unusable.

    The returned dict is safe to store and to pass to the renderers:

        {"name", "status", "checks": [{"group", "name", "status", "detail"}],
         "covers": [...], "defined_by": [...], "truncated": bool}
    """
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_REPORT_BYTES:
            return None
        try:
            raw = json.loads(bytes(raw).decode("utf-8", errors="replace"))
        except ValueError:
            return None
    if not isinstance(raw, dict) or raw.get("schema") != SUPPORTED_SCHEMA:
        return None

    raw_checks = raw.get("checks")
    if not isinstance(raw_checks, list):
        return None

    checks: List[Dict[str, str]] = []
    for item in raw_checks[:_MAX_CHECKS]:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        name = _clean(item.get("name"), _MAX_NAME_CHARS)
        if status not in _CHECK_STATUSES or not name:
            continue
        group = _GROUP_RE.sub("", _clean(item.get("group"), _MAX_GROUP_CHARS).lower())
        checks.append({
            "group": group.strip() or "checks",
            "name": name,
            "status": status,
            "detail": _clean(item.get("detail"), _MAX_DETAIL_CHARS),
        })
    if not checks:
        return None

    # The status is recomputed, never trusted: a report cannot claim "passed"
    # over a failed check. (It can still lie about the checks themselves, which
    # is why the comment says who produced the result.)
    claimed_pass = raw.get("status") == STATUS_PASSED
    any_fail = any(check["status"] == "fail" for check in checks)
    any_pass = any(check["status"] == "pass" for check in checks)
    status = STATUS_PASSED if (claimed_pass and any_pass and not any_fail) else STATUS_FAILED

    return {
        "name": _clean(raw.get("name"), _MAX_NAME_CHARS) or "Verification",
        "status": status,
        "checks": checks,
        "covers": _clean_patterns(raw.get("covers")),
        "defined_by": _clean_patterns(raw.get("defined_by")),
        "truncated": len(raw_checks) > _MAX_CHECKS,
    }


def final_status(report: Dict[str, Any], conclusion: Optional[str]) -> str:
    """Combine the report with what GitHub recorded for the workflow run.

    Both must agree on a pass. GitHub's conclusion is the harder of the two to
    fake from inside the artifact, so it can only ever make the result worse.
    """
    if report.get("status") == STATUS_PASSED and conclusion == "success":
        return STATUS_PASSED
    return STATUS_FAILED


# ---------------------------------------------------------------------------
# What the result does and does not speak for
# ---------------------------------------------------------------------------


def _matches(path: str, pattern: str) -> bool:
    """``dir/**`` covers everything under ``dir``; otherwise a shell-style glob."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def _matching(paths: List[str], patterns: List[str]) -> List[str]:
    return [p for p in paths if any(_matches(p, pattern) for pattern in patterns)]


def uncovered_paths(changed_paths: List[str], covers: List[str]) -> List[str]:
    """Changed files the run did not exercise, in the order given.

    Empty when the report declares no coverage: without a declaration there is
    nothing to compare against, and guessing would be worse than saying nothing.
    """
    if not covers:
        return []
    return [p for p in changed_paths if not any(_matches(p, pattern) for pattern in covers)]


def verification_changes(changed_paths: List[str], defined_by: List[str]) -> List[str]:
    """Changed files that define the verification itself.

    Best effort by construction: ``defined_by`` comes from the PR's own report,
    so a PR could drop entries from it. Workflow files are always included
    because they are fixed by GitHub, not declared by the report.
    """
    return _matching(changed_paths, list(_ALWAYS_DEFINED_BY) + list(defined_by))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Make untrusted text inert inside a one-line markdown table cell.

    Stops it closing the cell, opening HTML (including a forged Aurora marker),
    forming a link, autolinking a URL, or notifying a user by mention.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "'")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("://", ":/​/")
        .replace("www.", "www​.")
        .replace("@", "@​")
    )


def _code(value: Any) -> str:
    """Render an untrusted path as inline code (file names are author-controlled)."""
    cleaned = _clean(value, _MAX_PATH_CHARS).replace("`", "'")
    return f"`{cleaned}`" if cleaned else "`?`"


def _path_list(paths: List[str]) -> List[str]:
    lines = [f"- {_code(path)}" for path in paths[:_MAX_LISTED_PATHS]]
    if len(paths) > _MAX_LISTED_PATHS:
        lines.append(f"- …and {len(paths) - _MAX_LISTED_PATHS} more")
    return lines


def _group_rows(checks: List[Dict[str, str]]) -> List[str]:
    """One summary row per group, in first-seen order."""
    order: List[str] = []
    counts: Dict[str, Dict[str, int]] = {}
    for check in checks:
        group = check["group"]
        if group not in counts:
            order.append(group)
            counts[group] = {status: 0 for status in _CHECK_STATUSES}
        counts[group][check["status"]] += 1
    return [
        f"| {_text(group)} | {counts[group]['pass']} | {counts[group]['warn']} "
        f"| {counts[group]['fail']} | {counts[group]['skip']} |"
        for group in order
    ]


def encode_marker(head_sha: str) -> str:
    """Hidden marker naming the commit a verification comment refers to.

    A debugging aid, like the change-gating markers. It is never read back to
    decide anything: comment ids are tracked in ``verification_runs``, because
    a marker alone is not proof of authorship (anyone can paste one).
    """
    return f"<!-- {_MARKER_PREFIX}:v1 {head_sha} -->"


def render_comment(
    report: Dict[str, Any],
    *,
    status: str,
    head_sha: str,
    run_url: Optional[str],
    conclusion: Optional[str],
    changed_file_count: int,
    uncovered: List[str],
    changes_to_verification: List[str],
) -> str:
    """Render the pull request comment for one verification run.

    Deliberately deterministic. A green run needs no model to explain it, and a
    fixed template is what makes the comment auditable.
    """
    checks = report["checks"]
    passed = status == STATUS_PASSED
    short_sha = head_sha[:7]

    headline = f"**{'Passed' if passed else 'Failed'}** · commit `{short_sha}`"
    if run_url and run_url.startswith("https://github.com/"):
        headline += f" · [workflow run]({run_url})"

    lines = [
        "## Aurora Verification",
        "",
        headline,
        "",
        f"{_text(report['name'])} booted this pull request and checked it, "
        "rather than only reading the diff.",
    ]

    if not passed and report.get("status") == STATUS_PASSED:
        # The report claims a pass but the workflow run did not succeed.
        lines += [
            "",
            f"The checks below report success, but the workflow run ended as "
            f"`{_clean(conclusion, 32) or 'unknown'}`, so this counts as failed.",
        ]

    lines += [
        "",
        "| Stage | Passed | Warnings | Failed | Skipped |",
        "|---|---|---|---|---|",
        *_group_rows(checks),
    ]
    if report.get("truncated"):
        lines += ["", f"Only the first {_MAX_CHECKS} checks are shown."]

    failed = [check for check in checks if check["status"] == "fail"]
    if failed:
        lines += ["", "### Failed checks", "", "| Stage | Check | Detail |", "|---|---|---|"]
        for check in failed[:_MAX_FAILED_ROWS]:
            lines.append(
                f"| {_text(check['group'])} | {_text(check['name'])} "
                f"| {_text(check['detail']) or '-'} |"
            )
        if len(failed) > _MAX_FAILED_ROWS:
            lines.append(f"| … | …and {len(failed) - _MAX_FAILED_ROWS} more | |")

    if uncovered:
        lines += [
            "",
            "### Not exercised by this run",
            "",
            f"{len(uncovered)} of {changed_file_count} changed files are outside "
            "what this run boots, so the result says nothing about them:",
            "",
            *_path_list(uncovered),
        ]
    elif report.get("covers") and changed_file_count:
        lines += ["", f"All {changed_file_count} changed files are inside what this run boots."]

    if changes_to_verification:
        lines += [
            "",
            "### This pull request changes the verification itself",
            "",
            "The result above was produced by checks this pull request modifies. "
            "Review these changes before relying on it:",
            "",
            *_path_list(changes_to_verification),
        ]

    lines += [
        "",
        "---",
        "*Reported by this repository's own workflow and recorded by Aurora. "
        "Advisory only; it does not block merge.*",
        "",
        encode_marker(head_sha),
    ]
    return "\n".join(lines)

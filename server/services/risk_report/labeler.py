"""Link reverts to the PRs they undid, and classify PRs by review lane.

Pure functions, no I/O. The revert link is the backtest's answer key: it is
used to score the reviewer, never shown to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Files in Aurora's review lane (infra / deploy / CI / config / migrations),
# mirroring WHAT TO FLAG in services.change_gating.verdict._REVIEW_PROMPT.
_INFRA_PATH = re.compile(
    r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*|Makefile|Chart\.yaml|values[^/]*\.ya?ml"
    r"|\.env[^/]*|[^/]*\.tf|[^/]*\.tfvars|nginx[^/]*\.conf)$"
    r"|(^|/)(\.github/workflows|helm|charts|deploy|k8s|kubernetes|terraform|migrations?|infra|config)/",
    re.IGNORECASE,
)

# "Reverts owner/repo#N", "Reverts #N", "Revert #N", "reverts .../pull/N"
_REVERTS_REF = re.compile(
    r"\brevert\w*[^\n]{0,80}?(?:(?P<repo>[\w.-]+/[\w.-]+)?#(?P<num>\d+)|/pull/(?P<url_num>\d+))",
    re.IGNORECASE,
)
# "This reverts commit <sha>", "fully reverts commit <sha>"
_REVERTS_SHA = re.compile(r"\brevert(?:s|ed)?\s+(?:the\s+)?commit\s+([0-9a-f]{7,40})", re.IGNORECASE)
_REVERT_WORD = re.compile(r"\brevert", re.IGNORECASE)


@dataclass
class Evidence:
    """What happened after merge: the revert that undid the PR."""

    kind: str  # "revert_pr"
    ref: str  # "#136590"
    url: str
    title: str
    after_hours: float
    reason: str = ""  # the revert's own explanation, e.g. "Cache seems to be ever growing"


@dataclass
class LabeledPR:
    pr: Dict[str, Any]
    label: str  # "incident" (later reverted) | "clean" (control)
    evidence: List[Evidence] = field(default_factory=list)


def find_revert_target(revert_pr: Dict[str, Any], repo_full_name: str) -> Optional[Tuple[str, Any]]:
    """Which change did this revert PR undo?

    Returns ``("pr", number)``, ``("sha", sha)`` or None. Handles the four
    forms seen on grafana/grafana: GitHub's ``Reverts owner/repo#N`` button
    text, ``Reverts #N``, ``This reverts commit <sha>``, and a ``#N`` or
    ``/pull/N`` link next to the word revert (title or body).
    """
    for text in (revert_pr.get("body") or "", revert_pr.get("title") or ""):
        for m in _REVERTS_REF.finditer(text):
            repo = m.group("repo")
            if repo and repo.lower() != repo_full_name.lower():
                continue
            num = int(m.group("num") or m.group("url_num"))
            if num != revert_pr.get("number"):
                return ("pr", num)
        m = _REVERTS_SHA.search(text)
        if m:
            return ("sha", m.group(1))
    return None


def is_revert_itself(pr: Dict[str, Any]) -> bool:
    """Reverts are outcomes, not candidates to review or controls."""
    return bool(_REVERT_WORD.search(pr.get("title") or ""))


def touches_infra(files: List[Dict[str, Any]]) -> bool:
    return any(_INFRA_PATH.search(f.get("filename") or "") for f in files)


_LINK_TOKEN = re.compile(r"#\d+|commit [0-9a-f]{7,40}|/pull/\d+", re.IGNORECASE)


def _is_link_line(line: str) -> bool:
    """``Reverts owner/repo#N`` / ``This PR fully reverts commit <sha>.``: short,
    starts with a revert verb, carries a link. Plain checks, no backtracking."""
    lower = line.lower()
    return "revert" in lower[:30] and len(line.split()) <= 8 and bool(_LINK_TOKEN.search(lower))


def revert_reason(revert_pr: Dict[str, Any], limit: int = 400) -> str:
    """The human explanation in a revert PR body, minus the boilerplate link line."""
    lines = []
    for line in (revert_pr.get("body") or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or _is_link_line(stripped):
            continue
        lines.append(stripped)
    text = " ".join(lines)
    text = re.sub(r"<!--.*?-->", "", text).strip()
    return text[:limit]

"""Revert replay: a first Incident Prevention result from git history alone.

Today a team that installs the GitHub App sees nothing until someone opens
the next pull request on an enrolled repo, and even then a SAFE/RISKY
comment on a PR nobody has shipped yet proves little. But every active repo
already holds labelled incidents: the pull requests the team merged and
then had to revert. A revert is an incident report the team wrote for free,
with the culprit diff attached.

Replay finds the most recent reverted PRs on the default branch, plus a
control set of merged PRs that were never reverted, and runs the normal
pre-merge review on each original diff as it stood before merge. The result
is a scorecard on the team's own history ("Aurora would have flagged 3 of
your last 5 reverted PRs, and 1 of 5 PRs that shipped fine") in the time it
takes to review ten diffs, with no extra permission beyond what Incident
Prevention already asks for.

Replay never writes to GitHub: it only reads PRs, files and diffs.

This module is pure: revert detection, sample selection, the replay prompt,
scoring and the report. The GitHub reads live in ``github_adapter``; the
agent run lives in ``tasks.change_gating_replay`` (installed repos) and
``services.change_gating.replay_cli`` (any public repo, nothing installed).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# A PR is a revert when its title says so. Covers the GitHub "Revert" button
# (``Revert "feat: x"``), conventional commits (``revert: ...``,
# ``revert(scope): ...``, ``fix(scope): revert ...``) and prefixed titles
# (``PanelEditNext: Revert #125773``, ``[lts] Revert "..."``).
_REVERT_TITLE_RE = re.compile(r"(?:^|[\s:\]\(\[])revert(?:s|ed|ing)?\b", re.IGNORECASE)

# ``Reverts owner/repo#123``, ``Reverts #123``, ``Reverts [#123](...)``,
# ``This reverts https://github.com/owner/repo/pull/123``, ``Revert part of #123``.
_REVERTS_REF_RE = re.compile(
    r"\breverts?\b(?:\s+part\s+of)?\s+\[?"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)#(?P<num>\d+)"
    r"|#(?P<num2>\d+)"
    r"|https?://github\.com/(?P<repo3>[\w.-]+/[\w.-]+)/pull/(?P<num3>\d+))",
    re.IGNORECASE,
)
# Squash-merged original carried in the quoted title: ``Revert "feat: x (#123)"``.
_QUOTED_SQUASH_RE = re.compile(r'"[^"]*\(#(?P<num>\d+)\)[^"]*"')
# ``Revert "<original title>"`` (the GitHub button / ``git revert`` default).
_QUOTED_TITLE_RE = re.compile(r'revert(?:s|ed)?:?\s+"(?P<title>.+)"', re.IGNORECASE)
# ``This reverts commit <sha>`` (``git revert`` default message body).
_REVERTS_SHA_RE = re.compile(r"this reverts commit (?P<sha>[0-9a-f]{7,40})", re.IGNORECASE)
# Any single PR reference, used only as a last resort inside a revert PR.
_ANY_PR_REF_RE = re.compile(
    r"(?:https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)|(?<![\w/])#(?P<num2>\d+))"
)

# A revert must reach production before it counts: only PRs merged into the
# default branch. Controls must have had time to be reverted.
DEFAULT_CONTROL_MIN_AGE_DAYS = 7


@dataclass
class ReplayCase:
    """One PR to replay. ``label`` is ``reverted`` or ``control``."""

    pr_number: int
    title: str
    label: str
    merged_at: Optional[str] = None
    # For reverted cases: the PR that reverted it, how we matched it, and the
    # revert's own description, which usually says what actually broke.
    revert_pr_number: Optional[int] = None
    match_rule: Optional[str] = None
    revert_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayResult:
    """The review verdict for one case (or why there is none)."""

    case: ReplayCase
    status: str  # completed | error | skipped
    verdict: Optional[str] = None  # SAFE | RISKY
    summary: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["case"] = self.case.to_dict()
        return data


# ---------------------------------------------------------------------------
# Revert detection
# ---------------------------------------------------------------------------


def is_revert_title(title: Optional[str]) -> bool:
    return bool(title) and bool(_REVERT_TITLE_RE.search(title))


def _same_repo(ref_repo: Optional[str], repo_full_name: str) -> bool:
    return ref_repo is None or ref_repo.lower() == repo_full_name.lower()


def find_revert_target(
    revert_pr: Dict[str, Any],
    repo_full_name: str,
    by_title: Optional[Dict[str, int]] = None,
    by_merge_sha: Optional[Dict[str, int]] = None,
) -> Optional[tuple[int, str]]:
    """Return ``(reverted_pr_number, rule)`` for a revert PR, or ``None``.

    Rules run from most to least explicit, and the rule that matched is kept
    in the report so a reader can see why a PR was counted:

    1. ``reverts_ref``: ``Reverts owner/repo#N`` and its spellings.
    2. ``quoted_squash``: ``Revert "title (#N)"`` from a squash-merged original.
    3. ``reverts_sha``: ``This reverts commit <sha>`` matched to a merge commit.
    4. ``quoted_title``: ``Revert "title"`` matched to a merged PR's exact title.
    5. ``single_ref``: the revert mentions exactly one PR of this repo.

    References to another repository never match.
    """
    title = revert_pr.get("title") or ""
    body = revert_pr.get("body") or ""
    text = f"{title}\n{body}"
    own_number = revert_pr.get("number")

    for m in _REVERTS_REF_RE.finditer(text):
        repo = m.group("repo") or m.group("repo3")
        num = m.group("num") or m.group("num2") or m.group("num3")
        if _same_repo(repo, repo_full_name) and int(num) != own_number:
            return int(num), "reverts_ref"

    m = _QUOTED_SQUASH_RE.search(title)
    if m and int(m.group("num")) != own_number:
        return int(m.group("num")), "quoted_squash"

    if by_merge_sha:
        for m in _REVERTS_SHA_RE.finditer(body):
            sha = m.group("sha").lower()
            for merge_sha, num in by_merge_sha.items():
                if merge_sha.lower().startswith(sha):
                    return num, "reverts_sha"

    if by_title:
        m = _QUOTED_TITLE_RE.search(title)
        if m:
            num = by_title.get(m.group("title").strip())
            if num is not None and num != own_number:
                return num, "quoted_title"

    refs = set()
    for m in _ANY_PR_REF_RE.finditer(body):
        repo = m.group("repo")
        num = m.group("num") or m.group("num2")
        if _same_repo(repo, repo_full_name) and int(num) != own_number:
            refs.add(int(num))
    if len(refs) == 1:
        return refs.pop(), "single_ref"
    return None


def _is_merged_into(pr: Dict[str, Any], default_branch: str) -> bool:
    return bool(pr.get("merged_at")) and ((pr.get("base") or {}).get("ref")) == default_branch


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_paragraphs(body: Optional[str], limit: int = 600) -> str:
    text = (body or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def select_cases(
    revert_prs: Iterable[Dict[str, Any]],
    merged_prs: Iterable[Dict[str, Any]],
    repo_full_name: str,
    default_branch: str,
    *,
    fetch_pull: Callable[[int], Optional[Dict[str, Any]]],
    reverted_limit: int = 5,
    control_limit: int = 5,
    now: Optional[datetime] = None,
    control_min_age_days: int = DEFAULT_CONTROL_MIN_AGE_DAYS,
) -> List[ReplayCase]:
    """Pick the most recent reverted PRs and a control set of PRs that shipped.

    ``revert_prs`` are merged PRs whose title mentions a revert and
    ``merged_prs`` the pool of recent merged PRs for controls, both newest
    first (``GitHubPRAdapter.search_merged_pulls``). Every revert's target is
    resolved offline first, so a PR that was reverted can never be picked as
    a control even when the reverted set is already full. Then targets are
    fetched with ``fetch_pull`` only until ``reverted_limit`` is reached.

    A target is skipped when it was itself a revert (a re-land: the revert
    was the mistake, not the original) or never merged into the default
    branch. Controls are merged, non-revert, human-authored PRs at least
    ``control_min_age_days`` old, so a revert would already have landed.
    """
    reverts = [
        p for p in revert_prs
        if isinstance(p, dict) and p.get("number") and p.get("merged_at")
        and is_revert_title(p.get("title"))
    ]
    pool = [p for p in merged_prs if isinstance(p, dict) and p.get("number") and p.get("merged_at")]
    known = reverts + pool
    by_title = {(p.get("title") or "").strip(): p["number"] for p in known}
    by_merge_sha = {p["merge_commit_sha"]: p["number"] for p in known if p.get("merge_commit_sha")}

    targets = []
    for pr in reverts:
        found = find_revert_target(pr, repo_full_name, by_title, by_merge_sha)
        if found is not None:
            targets.append((pr, found[0], found[1]))
    revert_numbers = {p["number"] for p in reverts}
    target_numbers = {number for _, number, _ in targets}

    reverted: List[ReplayCase] = []
    tried: set[int] = set()
    for revert_pr, number, rule in targets:
        if len(reverted) >= reverted_limit:
            break
        if number in tried:
            continue
        tried.add(number)
        original = fetch_pull(number)
        if not original or not _is_merged_into(original, default_branch):
            continue
        if is_revert_title(original.get("title")):
            continue
        reverted.append(
            ReplayCase(
                pr_number=number,
                title=original.get("title") or "",
                label="reverted",
                merged_at=original.get("merged_at"),
                revert_pr_number=revert_pr["number"],
                match_rule=rule,
                revert_reason=_first_paragraphs(revert_pr.get("body")),
            )
        )

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=control_min_age_days)
    controls: List[ReplayCase] = []
    for pr in pool:
        if len(controls) >= control_limit:
            break
        number = pr["number"]
        if number in target_numbers or number in revert_numbers:
            continue
        if is_revert_title(pr.get("title")):
            continue
        if ((pr.get("user") or {}).get("type")) == "Bot":
            continue
        base_ref = (pr.get("base") or {}).get("ref")
        if base_ref is not None and base_ref != default_branch:
            continue
        merged_at = _parse_ts(pr.get("merged_at"))
        if merged_at is None or merged_at > cutoff:
            continue
        controls.append(
            ReplayCase(
                pr_number=number,
                title=pr.get("title") or "",
                label="control",
                merged_at=pr.get("merged_at"),
            )
        )
    return reverted + controls


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REPLAY_NOTE = """HISTORICAL REPLAY:
This pull request was merged on {merged_at}. You are reviewing it exactly as
it stood BEFORE merge, to measure what a pre-merge review would have said.
Judge the change from its diff and from the repository as of its base commit
{base_sha}. Do NOT look at commits, pull requests, issues, alerts, incidents
or messages dated after {merged_at}: using hindsight makes the replay
worthless. Review it the same way you would review any open pull request."""

_NO_TOOLS_NOTE = """In this replay you have NO tools. Ignore the tool list above and
judge the change from the PR description and diff below alone. Return the
JSON verdict as your only output."""


def build_replay_prompt(
    repo_full_name: str,
    pr: Dict[str, Any],
    files: List[Dict[str, Any]],
    diff: Optional[str],
    *,
    tools_available: bool = True,
) -> str:
    """The normal review prompt with a replay note pinned in front of the PR.

    The note does not say whether the PR was reverted. Reverted and control
    PRs get the identical prompt, so a reviewer that flags everything scores
    badly on the controls.
    """
    from services.change_gating.verdict import build_review_prompt

    prompt = build_review_prompt(repo_full_name, pr, files, diff)
    notes = [
        _REPLAY_NOTE.format(
            merged_at=pr.get("merged_at") or "an unknown date",
            base_sha=(pr.get("base") or {}).get("sha") or "unknown",
        )
    ]
    if not tools_available:
        notes.append(_NO_TOOLS_NOTE)
    marker = "PR METADATA:"
    head, sep, tail = prompt.partition(marker)
    if not sep:
        return prompt + "\n\n" + "\n\n".join(notes)
    return head + "\n\n".join(notes) + "\n\n" + sep + tail


# ---------------------------------------------------------------------------
# Scoring and report
# ---------------------------------------------------------------------------


def score(results: List[ReplayResult]) -> Dict[str, Any]:
    """Count what a reader cares about. Errors are left out of every ratio."""
    done = [r for r in results if r.status == "completed" and r.verdict]
    reverted = [r for r in done if r.case.label == "reverted"]
    controls = [r for r in done if r.case.label == "control"]
    caught = [r for r in reverted if r.verdict == "RISKY"]
    false_alarms = [r for r in controls if r.verdict == "RISKY"]
    return {
        "reverted_reviewed": len(reverted),
        "reverted_flagged": len(caught),
        "controls_reviewed": len(controls),
        "controls_flagged": len(false_alarms),
        "errors": sum(1 for r in results if r.status != "completed"),
        "recall": round(len(caught) / len(reverted), 3) if reverted else None,
        "false_alarm_rate": round(len(false_alarms) / len(controls), 3) if controls else None,
    }


def headline(repo_full_name: str, scores: Dict[str, Any]) -> str:
    if not scores["reverted_reviewed"]:
        return (
            f"No reverted pull requests found on {repo_full_name}'s default branch "
            "in the recent history Aurora read."
        )
    line = (
        f"Aurora would have flagged {scores['reverted_flagged']} of your last "
        f"{scores['reverted_reviewed']} reverted PRs before merge"
    )
    if scores["controls_reviewed"]:
        line += (
            f", and {scores['controls_flagged']} of {scores['controls_reviewed']} "
            "PRs that shipped fine"
        )
    return line + "."


def build_report(
    repo_full_name: str,
    results: List[ReplayResult],
    *,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    mode: str = "agent",
) -> Dict[str, Any]:
    scores = score(results)
    report: Dict[str, Any] = {
        "repo_full_name": repo_full_name,
        "mode": mode,
        "headline": headline(repo_full_name, scores),
        "scores": scores,
        "results": [r.to_dict() for r in results],
    }
    if started_at:
        report["started_at"] = started_at.isoformat()
    if finished_at:
        report["finished_at"] = finished_at.isoformat()
    if started_at and finished_at:
        report["duration_seconds"] = round((finished_at - started_at).total_seconds(), 1)
    return report


def _cell(text: Any, limit: int = 160) -> str:
    value = " ".join(str(text or "").split()).replace("|", "\\|")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_markdown(report: Dict[str, Any]) -> str:
    """A short report a person reads in under a minute."""
    repo = report["repo_full_name"]
    lines = [f"## Revert replay: {repo}", "", f"**{report['headline']}**", ""]
    reverted = [r for r in report["results"] if r["case"]["label"] == "reverted"]
    controls = [r for r in report["results"] if r["case"]["label"] == "control"]
    if reverted:
        lines += [
            "### Reverted PRs",
            "",
            "| PR | Reverted by | Aurora before merge | Top finding | Why it was reverted |",
            "|---|---|---|---|---|",
        ]
        for r in reverted:
            case = r["case"]
            top = (r.get("findings") or [{}])[0]
            lines.append(
                f"| #{case['pr_number']} {_cell(case['title'], 60)} "
                f"| #{case['revert_pr_number']} | {_verdict_cell(r)} "
                f"| {_cell(top.get('title'), 90)} | {_cell(case.get('revert_reason'), 140)} |"
            )
        lines.append("")
    if controls:
        lines += ["### Control PRs (merged, never reverted)", "", "| PR | Aurora before merge | Top finding |", "|---|---|---|"]
        for r in controls:
            case = r["case"]
            top = (r.get("findings") or [{}])[0]
            lines.append(
                f"| #{case['pr_number']} {_cell(case['title'], 60)} | {_verdict_cell(r)} "
                f"| {_cell(top.get('title'), 90)} |"
            )
        lines.append("")
    s = report["scores"]
    lines.append(
        f"Reviewed {s['reverted_reviewed'] + s['controls_reviewed']} PRs"
        + (f" in {report['duration_seconds']}s" if report.get("duration_seconds") is not None else "")
        + (f", {s['errors']} errored" if s["errors"] else "")
        + ". Replay only reads PRs and diffs; it posts nothing to GitHub."
    )
    return "\n".join(lines)


def _verdict_cell(result: Dict[str, Any]) -> str:
    if result.get("status") != "completed" or not result.get("verdict"):
        return f"error ({_cell(result.get('error'), 40)})"
    return "**RISKY**" if result["verdict"] == "RISKY" else "SAFE"


# ---------------------------------------------------------------------------
# Replay loop
# ---------------------------------------------------------------------------

DEFAULT_REVERTED = 5
DEFAULT_CONTROLS = 5


def collect_cases(
    reader,
    repo_full_name: str,
    *,
    reverted_limit: int = DEFAULT_REVERTED,
    control_limit: int = DEFAULT_CONTROLS,
    now: Optional[datetime] = None,
    search_limit: int = 100,
) -> tuple[str, List[ReplayCase], Dict[int, Dict[str, Any]]]:
    """Read the repo and pick the cases: two searches plus one GET per target.

    Returns ``(default_branch, cases, fetched_prs)``; ``fetched_prs`` caches
    the full PR objects already read so the replay loop does not GET them
    twice.
    """
    now = now or datetime.now(timezone.utc)
    default_branch = reader.get_repo().get("default_branch") or "main"
    cutoff = (now - timedelta(days=DEFAULT_CONTROL_MIN_AGE_DAYS)).date().isoformat()
    revert_prs = reader.search_merged_pulls("in:title revert", limit=search_limit)
    pool = reader.search_merged_pulls(
        f"base:{default_branch} merged:<={cutoff}", limit=max(control_limit * 4, 20)
    )
    fetched: Dict[int, Dict[str, Any]] = {}

    def fetch(number: int) -> Optional[Dict[str, Any]]:
        if number not in fetched:
            pr = reader.find_pull_request(number)
            if pr is None:
                return None
            fetched[number] = pr
        return fetched[number]

    cases = select_cases(
        revert_prs, pool, repo_full_name, default_branch,
        fetch_pull=fetch, reverted_limit=reverted_limit,
        control_limit=control_limit, now=now,
    )
    return default_branch, cases, fetched


def run_replay(
    reader,
    repo_full_name: str,
    *,
    review: Callable[[Dict[str, Any], List[Dict[str, Any]], Optional[str]], Dict[str, Any]],
    reverted_limit: int = DEFAULT_REVERTED,
    control_limit: int = DEFAULT_CONTROLS,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    mode: str = "agent",
) -> Dict[str, Any]:
    """Provider-neutral replay loop, shared by the Celery task and the CLI.

    ``reader`` needs the GitHub read methods of ``GitHubPRAdapter``
    (``get_repo``, ``search_merged_pulls``, ``find_pull_request``,
    ``list_files``, ``get_diff``). ``review(pr, files, diff)`` returns a
    verdict dict or raises; one failing case is recorded as an error and the
    rest still run. Reverted cases run first: they are the result a new user
    is waiting for.
    """
    started = datetime.now(timezone.utc)
    default_branch, cases, fetched = collect_cases(
        reader, repo_full_name,
        reverted_limit=reverted_limit, control_limit=control_limit, now=started,
    )

    results: List[ReplayResult] = []
    for case in cases:
        t0 = time.monotonic()
        try:
            pr = fetched.get(case.pr_number) or reader.find_pull_request(case.pr_number)
            if pr is None:
                raise LookupError("pr_not_found")
            files = reader.list_files(case.pr_number)
            diff = reader.get_diff(case.pr_number)
            verdict = review(pr, files, diff)
            results.append(ReplayResult(
                case=case, status="completed",
                verdict=verdict.get("verdict"),
                summary=verdict.get("summary") or "",
                findings=verdict.get("findings") or [],
                seconds=round(time.monotonic() - t0, 1),
            ))
        except Exception as exc:
            logger.warning(
                "change_gating=replay repo=%s pr=%s status=case_failed error_class=%s",
                repo_full_name, case.pr_number, type(exc).__name__,
            )
            results.append(ReplayResult(
                case=case, status="error", error=str(exc)[:200] or type(exc).__name__,
                seconds=round(time.monotonic() - t0, 1),
            ))
        if on_progress is not None:
            partial = build_report(repo_full_name, results, started_at=started,
                                   finished_at=datetime.now(timezone.utc), mode=mode)
            partial["status"] = "running"
            partial["total_cases"] = len(cases)
            on_progress(partial)

    report = build_report(repo_full_name, results, started_at=started,
                          finished_at=datetime.now(timezone.utc), mode=mode)
    report["status"] = "completed"
    report["total_cases"] = len(cases)
    report["default_branch"] = default_branch
    return report

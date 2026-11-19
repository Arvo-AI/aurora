"""Build a Risk Report for one public repo: gather, sample, replay, score."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from services.risk_report.backtest import (
    HindsightFn,
    ReviewFn,
    explain_with_hindsight,
    review_one,
    run_reviewer,
    score,
)
from services.risk_report.labeler import (
    Evidence,
    LabeledPR,
    find_revert_target,
    is_revert_itself,
    revert_reason,
    touches_infra,
)
from services.risk_report.public_github import PublicGitHub, parse_pr, parse_repo, parse_ts

logger = logging.getLogger(__name__)

MAX_REVERT_WINDOW_DAYS = 30


def _is_bot(pr: Dict[str, Any]) -> bool:
    user = pr.get("user") or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def _original_of(gh: PublicGitHub, rv: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fetch the PR a revert undid, or None if it can't be resolved."""
    target = find_revert_target(rv, gh.full_name)
    if not target:
        return None
    number = target[1] if target[0] == "pr" else gh.pr_for_commit(target[1])
    if not number:
        return None
    try:
        return gh.get_pr(number)
    except Exception:
        return None


def _revert_evidence(original: Dict[str, Any], rv: Dict[str, Any]) -> Optional[Evidence]:
    """Evidence when ``rv`` is a valid revert of a merged, non-revert PR within the window."""
    orig_at, rv_at = parse_ts(original.get("merged_at")), parse_ts(rv.get("merged_at"))
    if not orig_at or not rv_at or rv_at <= orig_at or is_revert_itself(original):
        return None
    hours = (rv_at - orig_at).total_seconds() / 3600
    if hours > MAX_REVERT_WINDOW_DAYS * 24:
        return None
    return Evidence(
        kind="revert_pr", ref=f"#{rv['number']}", url=rv.get("html_url", ""),
        title=(rv.get("title") or "")[:200], after_hours=round(hours, 1),
        reason=revert_reason(rv),
    )


def gather_incidents(gh: PublicGitHub, days: int, max_incidents: int) -> List[LabeledPR]:
    """Merged PRs that a later merged PR reverted, newest revert first."""
    out: List[LabeledPR] = []
    seen = set()
    for rv in gh.search_merged_reverts(days=days):
        if len(out) >= max_incidents:
            break
        original = _original_of(gh, rv)
        if not original or original["number"] in seen:
            continue
        evidence = _revert_evidence(original, rv)
        if evidence:
            seen.add(original["number"])
            out.append(LabeledPR(pr=original, label="incident", evidence=[evidence]))
    return out


def gather_controls(
    gh: PublicGitHub, days: int, exclude: set, revert_numbers: set, limit: int
) -> List[LabeledPR]:
    """Recent merged human PRs nobody reverted: the false-alarm baseline."""
    out = []
    for pr in gh.list_merged_prs(days=days, max_pages=1):
        if len(out) >= limit:
            break
        if pr["number"] in exclude or pr["number"] in revert_numbers:
            continue
        if is_revert_itself(pr) or _is_bot(pr):
            continue
        out.append(LabeledPR(pr=pr, label="clean"))
    return out


def build_report(
    repo_url: str,
    user_id: str,
    days: int = 60,
    max_incidents: int = 5,
    max_controls: int = 5,
    review_fn: ReviewFn = run_reviewer,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    gh: Optional[PublicGitHub] = None,
    hindsight_fn: Optional[HindsightFn] = explain_with_hindsight,
) -> Dict[str, Any]:
    started = time.monotonic()
    owner, repo = parse_repo(repo_url)
    gh = gh or PublicGitHub(owner, repo)
    gh.get_repo()  # 404s early on private / missing repos

    def progress(stage: str, **extra):
        if on_progress:
            on_progress({"stage": stage, "elapsed_s": round(time.monotonic() - started, 1), **extra})

    progress("finding_reverts")
    # Look at 2x reverts, then prefer ones in Aurora's lane (infra/CI/config):
    # the reviewer is told to ignore app-code bugs, so a reverted UI change is
    # out of scope by design, not a miss.
    incident_pool = gather_incidents(gh, days, max_incidents * 2)
    reverts = {int(e.ref[1:]) for lp in incident_pool for e in lp.evidence}

    progress("finding_controls", incidents=len(incident_pool))
    # Pull 2x candidates, then prefer controls in Aurora's lane (infra/CI/config)
    # so the false-alarm rate is measured on comparable changes.
    candidates = gather_controls(
        gh, days, {lp.pr["number"] for lp in incident_pool}, reverts, max_controls * 2
    )

    files_by_pr: Dict[int, List[Dict[str, Any]]] = {}
    for lp in incident_pool + candidates:
        files_by_pr[lp.pr["number"]] = gh.list_pr_files(lp.pr["number"])

    def in_lane_first(lp: LabeledPR) -> bool:
        return not touches_infra(files_by_pr[lp.pr["number"]])  # False sorts first

    incidents = sorted(incident_pool, key=in_lane_first)[:max_incidents]
    controls = sorted(candidates, key=in_lane_first)[:max_controls]

    sample = incidents + controls
    results = []
    for i, lp in enumerate(sample, 1):
        progress("reviewing", done=i - 1, total=len(sample), pr=lp.pr["number"])
        files = files_by_pr[lp.pr["number"]]
        results.append(review_one(
            user_id, gh.full_name, lp, files, touches_infra(files), review_fn,
            hindsight_fn=hindsight_fn,
        ))

    report = {
        "mode": "repo",
        "repo": gh.full_name,
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "github_requests": gh.requests_made,
        "score": score(results),
        # Infra / CI / config / migration changes: what Aurora is told to judge.
        "score_in_lane": score([r for r in results if r.infra_files]),
        "prs": [r.to_dict() for r in results],
    }
    progress("done")
    return report


def find_revert_of(gh: PublicGitHub, pr: Dict[str, Any]) -> Optional[Evidence]:
    """Was this merged PR later reverted? Returns the evidence, or None."""
    merged_at = parse_ts(pr.get("merged_at"))
    if not merged_at:
        return None
    for rv in gh.search_reverts_of(pr["number"]):
        rv_at = parse_ts(rv.get("merged_at"))
        if not rv_at or rv_at <= merged_at or rv.get("number") == pr["number"]:
            continue
        if find_revert_target(rv, gh.full_name) != ("pr", pr["number"]):
            continue
        return Evidence(
            kind="revert_pr", ref=f"#{rv['number']}", url=rv.get("html_url", ""),
            title=(rv.get("title") or "")[:200],
            after_hours=round((rv_at - merged_at).total_seconds() / 3600, 1),
            reason=revert_reason(rv),
        )
    return None


def build_pr_report(
    pr_url: str,
    user_id: str,
    review_fn: ReviewFn = run_reviewer,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    gh: Optional[PublicGitHub] = None,
    hindsight_fn: Optional[HindsightFn] = explain_with_hindsight,
) -> Dict[str, Any]:
    """Review ONE PR the user picked, as it stood before merge.

    The user usually knows whether this PR caused trouble, so they hold the
    answer key. For a merged PR we also look for a later revert and show it
    next to the verdict; an open PR is simply a live pre-merge review.
    """
    started = time.monotonic()
    owner, repo, number = parse_pr(pr_url)
    gh = gh or PublicGitHub(owner, repo)
    gh.get_repo()

    def progress(stage: str, **extra):
        if on_progress:
            on_progress({"stage": stage, "elapsed_s": round(time.monotonic() - started, 1), **extra})

    progress("fetching_pr")
    pr = gh.get_pr(number)
    files = gh.list_pr_files(number)
    if pr.get("merged_at"):
        progress("checking_what_happened")
        evidence = find_revert_of(gh, pr)
        labeled = LabeledPR(pr=pr, label="incident" if evidence else "unknown",
                            evidence=[evidence] if evidence else [])
    else:
        labeled = LabeledPR(pr=pr, label="open")

    progress("reviewing", done=0, total=1, pr=number)
    result = review_one(
        user_id, gh.full_name, labeled, files, touches_infra(files), review_fn,
        hindsight_fn=hindsight_fn,
    )
    report = {
        "mode": "pr",
        "repo": gh.full_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.monotonic() - started, 1),
        "github_requests": gh.requests_made,
        "score": score([result]),
        "score_in_lane": score([result] if result.infra_files else []),
        "prs": [result.to_dict()],
    }
    progress("done")
    return report

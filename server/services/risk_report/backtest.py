"""Replay Aurora's pre-merge reviewer on past PRs and score it.

No-hindsight rule: the reviewer sees exactly what a reviewer could have seen
the moment before merge (title, description, author, branches, changed files
and their patches). It never sees the merge outcome, comments, reviews, the
revert, or any later commit. ``build_backtest_prompt`` is the single place
that assembles the reviewer's input, and the tests pin that it carries no
post-merge text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from services.change_gating.verdict import build_review_prompt
from services.risk_report.labeler import LabeledPR

logger = logging.getLogger(__name__)

BACKTEST_NOTE = """BACKTEST MODE:
You are reviewing this pull request exactly as it stood immediately BEFORE it
merged (base commit {base_sha}). Judge it only from the diff below and the
repository as of that commit. No monitoring, deployment, or chat integrations
are connected for this repository, so do not wait on or cite live signals.
Apply the same VERDICT DECISION TEST and return the same final JSON verdict."""

_POST_MERGE_LINE = re.compile(
    r"\b(reverted|rolled back|rollback)\s+(in|by|via)\b", re.IGNORECASE
)


def pre_merge_snapshot(pr: Dict[str, Any], scrub_refs: List[str]) -> Dict[str, Any]:
    """Whitelist the PR fields that existed before merge.

    PR descriptions can be edited after merge ("reverted in #418"), so any
    line naming a later outcome, or one of the evidence refs, is dropped.
    """
    body_lines = []
    for line in (pr.get("body") or "").splitlines():
        if _POST_MERGE_LINE.search(line):
            continue
        if any(ref and ref in line for ref in scrub_refs):
            continue
        body_lines.append(line)
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "body": "\n".join(body_lines),
        "user": {"login": (pr.get("user") or {}).get("login")},
        "base": {"ref": base.get("ref"), "sha": base.get("sha")},
        "head": {"ref": head.get("ref"), "sha": head.get("sha")},
    }


def build_backtest_prompt(
    repo_full_name: str, labeled: LabeledPR, files: List[Dict[str, Any]]
) -> str:
    refs = [e.ref for e in labeled.evidence] + [e.url for e in labeled.evidence]
    snapshot = pre_merge_snapshot(labeled.pr, scrub_refs=refs)
    files = [
        {k: f.get(k) for k in ("filename", "status", "additions", "deletions", "patch")}
        for f in files
    ]
    prompt = build_review_prompt(repo_full_name, snapshot, files)
    note = BACKTEST_NOTE.format(base_sha=snapshot["base"]["sha"] or "unknown")
    return note + "\n\n" + prompt


@dataclass
class PRResult:
    number: int
    title: str
    url: str
    merged_at: str
    label: str  # "incident" (reverted) | "clean" (control) | "unknown" (user-picked, not reverted) | "open"
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    infra_files: bool = False
    verdict: Optional[str] = None  # "RISKY" | "SAFE" | None on error
    summary: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    seconds: float = 0.0
    session_id: Optional[str] = None  # the review's Aurora chat, for "see full reasoning"
    files_reviewed: List[str] = field(default_factory=list)
    # Written AFTER the verdict, with the revert in view. Never fed back to the reviewer.
    hindsight: Optional[Dict[str, Any]] = None

    @property
    def outcome(self) -> str:
        if self.verdict is None:
            return "error"
        if self.label in ("unknown", "open"):  # no answer key: the user judges
            return "flagged" if self.verdict == "RISKY" else "not_flagged"
        if self.label == "incident":
            return "caught" if self.verdict == "RISKY" else "missed"
        return "false_alarm" if self.verdict == "RISKY" else "quiet"

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "outcome": self.outcome}


def score(results: List[PRResult]) -> Dict[str, Any]:
    counts = {"caught": 0, "missed": 0, "false_alarm": 0, "quiet": 0, "error": 0,
              "flagged": 0, "not_flagged": 0}
    for r in results:
        counts[r.outcome] += 1
    incidents = counts["caught"] + counts["missed"]
    flagged = counts["caught"] + counts["false_alarm"]
    clean = counts["false_alarm"] + counts["quiet"]
    return {
        **counts,
        "incidents_reviewed": incidents,
        "clean_reviewed": clean,
        "recall": round(counts["caught"] / incidents, 2) if incidents else None,
        "precision": round(counts["caught"] / flagged, 2) if flagged else None,
        "false_alarm_rate": round(counts["false_alarm"] / clean, 2) if clean else None,
    }


def run_reviewer(user_id: str, repo_full_name: str, number: int, prompt: str, rail_text: str) -> Dict[str, Any]:
    """Run the same agent chain ``tasks.change_gating`` uses, with no GitHub writes.

    Returns the normalized verdict dict (``verdict``/``summary``/``findings``).
    """
    from chat.background.task import create_background_chat_session, run_background_chat
    from services.change_gating.verdict import extract_verdict_with_llm, parse_verdict
    from tasks.change_gating import _read_final_assistant_message

    trigger_metadata = {"source": "risk_report", "repo": repo_full_name, "pr_number": number}
    session_id = create_background_chat_session(
        user_id=user_id,
        title=f"Backtest: {repo_full_name}#{number}",
        trigger_metadata=trigger_metadata,
    )
    result = run_background_chat.apply(kwargs={
        "user_id": user_id,
        "session_id": session_id,
        "initial_message": prompt,
        "trigger_metadata": trigger_metadata,
        "send_notifications": False,
        "mode": "ask",
        "rail_text": rail_text,
    }).result
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise RuntimeError("agent_failed")
    if result.get("guardrail_blocked"):
        raise RuntimeError("guardrail_blocked")
    final_text = _read_final_assistant_message(user_id, session_id)
    verdict = parse_verdict(final_text) or extract_verdict_with_llm(final_text)
    if not verdict:
        raise RuntimeError("verdict_parse_failed")
    # Same normalization as change_gating: RISKY needs a finding to count.
    if verdict.get("verdict") == "RISKY" and not verdict.get("findings"):
        verdict["verdict"] = "SAFE"
    if verdict.get("verdict") == "SAFE":
        verdict["findings"] = []
    verdict["session_id"] = session_id
    return verdict


ReviewFn = Callable[[str, str, int, str, str], Dict[str, Any]]
HindsightFn = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]

_HINDSIGHT_PROMPT = """You are writing a short post-incident note for an engineer.
A pre-merge reviewer (Aurora) judged a pull request BEFORE it merged. Afterwards the
team reverted it. Compare the two. Be concrete and brief; do not speculate beyond the
inputs. If the revert reason is vague, say so.

PR: {title}
Files changed: {files}
Diff excerpt (truncated):
{diff}

Reviewer verdict before merge: {verdict}
Reviewer summary: {summary}
Reviewer findings: {findings}

What happened after merge: reverted in {ref}, {hours} hours later.
Revert title: {revert_title}
Revert reason as written by the team: {reason}

Return:
- what_broke: one sentence, what went wrong in production/CI per the revert.
- visible_in_diff: true if the problem is visible in the diff itself, false if it only shows up at runtime/scale.
- verdict_gap: one sentence on why the reviewer's verdict did or did not match the outcome.
- would_catch_it: one sentence naming the signal or context that would have caught it before merge (e.g. CI cache size history, a load test, a dependency's consumers)."""


_ONE_SENTENCE = "One sentence"


def explain_with_hindsight(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One structured LLM call comparing the verdict to what actually happened.

    Runs after ``review_one`` has fixed the verdict; it may see the revert.
    Returns None on any failure (the report still renders without it).
    """
    try:
        from pydantic import BaseModel, Field

        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.providers import create_chat_model

        class Hindsight(BaseModel):
            what_broke: str = Field(description=_ONE_SENTENCE)
            visible_in_diff: bool
            verdict_gap: str = Field(description=_ONE_SENTENCE)
            would_catch_it: str = Field(description=_ONE_SENTENCE)

        llm = create_chat_model(ModelConfig.MAIN_MODEL, temperature=0.0, streaming=False)
        out = llm.with_structured_output(Hindsight, include_raw=True, method="function_calling").invoke(
            _HINDSIGHT_PROMPT.format(**ctx)
        )
        parsed = out.get("parsed") if isinstance(out, dict) else out
        return parsed.model_dump() if parsed is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("risk_report hindsight failed: %s", exc)
        return None


def _hindsight_context(labeled: LabeledPR, files: List[Dict[str, Any]], out: "PRResult") -> Dict[str, Any]:
    ev = labeled.evidence[0]
    diff = "\n".join(
        f"--- {f.get('filename')}\n{(f.get('patch') or '')[:1500]}" for f in files[:6]
    )[:6000]
    return {
        "title": out.title,
        "files": ", ".join(out.files_reviewed[:20]),
        "diff": diff,
        "verdict": out.verdict,
        "summary": out.summary,
        "findings": "; ".join(f"{f.get('severity')} {f.get('title')}" for f in out.findings) or "none",
        "ref": ev.ref,
        "hours": ev.after_hours,
        "revert_title": ev.title,
        "reason": ev.reason or "(no reason written)",
    }


def review_one(
    user_id: str,
    repo_full_name: str,
    labeled: LabeledPR,
    files: List[Dict[str, Any]],
    infra: bool,
    review_fn: ReviewFn = run_reviewer,
    clock: Callable[[], float] = None,
    hindsight_fn: Optional[HindsightFn] = explain_with_hindsight,
) -> PRResult:
    import time

    clock = clock or time.monotonic
    pr = labeled.pr
    out = PRResult(
        number=pr["number"],
        title=pr.get("title") or "",
        url=pr.get("html_url") or "",
        merged_at=pr.get("merged_at") or "",
        label=labeled.label,
        evidence=[asdict(e) for e in labeled.evidence],
        infra_files=infra,
        files_reviewed=[f.get("filename") or "" for f in files],
    )
    start = clock()
    try:
        prompt = build_backtest_prompt(repo_full_name, labeled, files)
        rail_text = f"{pr.get('title') or ''}\n\n{pr.get('body') or ''}"
        verdict = review_fn(user_id, repo_full_name, pr["number"], prompt, rail_text)
        out.verdict = verdict.get("verdict")
        out.summary = verdict.get("summary") or ""
        out.findings = verdict.get("findings") or []
        out.session_id = verdict.get("session_id")
    except Exception as exc:  # one bad PR must not sink the report
        logger.warning("risk_report pr=%s#%s error=%s", repo_full_name, pr["number"], exc)
        out.error = str(exc)[:200]
    out.seconds = round(clock() - start, 1)
    # Only after the verdict is final, and only where there is an outcome to compare.
    if hindsight_fn and out.verdict and labeled.label == "incident" and labeled.evidence:
        out.hindsight = hindsight_fn(_hindsight_context(labeled, files, out))
    return out

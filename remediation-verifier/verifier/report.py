"""The run as a JSON report.

A report is a projection of verdicts and capture metadata. It never reads the
cluster, never re-runs a check and never invents a value. `generated_at` is the
only volatile field and the caller supplies it, so the same evidence produces
the same bytes.

Every metric prints a numerator and a denominator, and a metric that has no
denominator is null instead of zero (R9). A positive verdict whose checks carry
no evidence is refused here as well as in the decision layer, because a report
is where a claim would be read by someone else (R3, R11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verifier.config import ENCODING
from verifier.decision import (
    EXECUTION_ERROR,
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
    Verdict,
)
from verifier.model import FAIL, INCONCLUSIVE, Contract

MODE_MIXED = "MIXED"
MODE_UNKNOWN = "UNKNOWN"

REPRODUCTION = "reproduction"
REMEDIATION = "remediation"


class ReportError(ValueError):
    """The report cannot be written without breaking an evidence rule."""


@dataclass(frozen=True)
class Run:
    """One finished variant: what it produced and what it decided."""

    verdict: Verdict
    mode: str
    captured_at: str
    timing: Mapping[str, float] = field(default_factory=dict)


def report_document(
    contract: Contract, runs: Sequence[Run], *, generated_at: str
) -> dict[str, Any]:
    """Return the report as JSON-ready data."""
    runs = tuple(runs)
    _reject_unbacked_positive(runs)
    return {
        "advisory": True,
        "scenario": contract.scenario,
        "title": contract.title,
        "generated_at": generated_at,
        "source": _source(contract),
        "mode": _mode(runs),
        "metrics": metrics_for(contract, runs),
        "variants": [_variant(run) for run in runs],
    }


def metrics_for(contract: Contract, runs: Sequence[Run]) -> dict[str, Any]:
    """Return every metric with its denominator, or null when there is none."""
    runs = tuple(runs)
    baseline = next((run for run in runs if run.verdict.kind == REPRODUCTION), None)
    detection = None
    weak = None
    if baseline is not None:
        results = baseline.verdict.results
        failed = sum(1 for result in results if result.status == FAIL)
        detection = {"failed": failed, "checks": len(results)}
        weak = {"passed": len(baseline.verdict.weak_checks), "checks": len(results)}

    remediation = [run for run in runs if run.verdict.kind == REMEDIATION]
    counts = {"verified": 0, "rejected": 0, "inconclusive": 0, "execution_error": 0}
    for run in remediation:
        status = run.verdict.status
        if status == VERIFIED_WITHIN_CONTRACT:
            counts["verified"] += 1
        elif status == REJECTED:
            counts["rejected"] += 1
        elif status == INCONCLUSIVE:
            counts["inconclusive"] += 1
        elif status == EXECUTION_ERROR:
            counts["execution_error"] += 1
    counts["runs"] = len(remediation)

    per_variant: dict[str, int] = {}
    for run in runs:
        per_variant[run.verdict.variant] = per_variant.get(run.verdict.variant, 0) + 1

    return {
        "fault_detection": detection,
        "weak_checks": weak,
        "remediation": counts,
        "runs_per_variant": per_variant,
    }


def write_report(
    contract: Contract,
    runs: Sequence[Run],
    path: Path | str,
    *,
    generated_at: str,
) -> Path:
    """Write the deterministic JSON report and return where it landed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = report_document(contract, runs, generated_at=generated_at)
    text = json.dumps(document, indent=2, sort_keys=True)
    target.write_text(f"{text}\n", encoding=ENCODING)
    return target


def _variant(run: Run) -> dict[str, Any]:
    verdict = run.verdict
    return {
        "variant": verdict.variant,
        "kind": verdict.kind,
        "mode": run.mode,
        "captured_at": run.captured_at,
        "status": verdict.status,
        "reproduction_status": verdict.reproduction_status,
        "reason": verdict.reason,
        "weak_checks": list(verdict.weak_checks),
        "unresolved": [
            {
                "id": element.id,
                "source": element.source,
                "reason": element.reason,
                "missing": list(element.missing),
            }
            for element in verdict.unresolved
        ],
        "checks": [
            {
                "id": result.id,
                "fault": result.fault,
                "status": result.status,
                "reason": result.reason,
                "source": result.source,
                "evidence": dict(result.evidence),
            }
            for result in verdict.results
        ],
        "timing": dict(run.timing),
    }


def _source(contract: Contract) -> dict[str, str]:
    source = contract.source
    return {
        "repo": source.repo,
        "url": source.url,
        "commit": source.commit,
        "fork_repo": source.fork_repo,
        "fork_commit": source.fork_commit,
    }


def _mode(runs: Sequence[Run]) -> str:
    modes = {run.mode for run in runs}
    if not modes:
        return MODE_UNKNOWN
    if len(modes) == 1:
        return next(iter(modes))
    return MODE_MIXED


def _reject_unbacked_positive(runs: Sequence[Run]) -> None:
    for run in runs:
        verdict = run.verdict
        if verdict.status != VERIFIED_WITHIN_CONTRACT:
            continue
        if not verdict.results:
            raise ReportError(
                f"the run {verdict.variant} claims a positive without any check result"
            )
        unsupported = [result.id for result in verdict.results if not result.evidence]
        if unsupported:
            raise ReportError(
                f"the run {verdict.variant} claims a positive without evidence for "
                f"{', '.join(unsupported)}"
            )

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
from verifier.model import FAIL, INCONCLUSIVE, PASS, Contract

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
        "metrics": metrics_for(runs),
        "variants": [_variant(run) for run in runs],
    }


def metrics_for(runs: Sequence[Run]) -> dict[str, Any]:
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


def render_markdown(contract: Contract, runs: Sequence[Run], *, generated_at: str) -> str:
    """Return the report as Markdown, without a trailing newline."""
    runs = tuple(runs)
    _reject_unbacked_positive(runs)
    metrics = metrics_for(runs)
    lines = ["# Remediation verification report", ""]
    lines.append("Advisory. This report is evidence for a human reviewer and never a merge gate.")
    lines.append("")
    lines += _metadata(contract, runs, generated_at)
    lines += ["## Metrics", ""]
    lines += _metrics_table(metrics)
    lines += ["## Variants", ""]
    lines += _variants_table(runs)
    seen: dict[str, int] = {}
    for run in runs:
        name = run.verdict.variant
        seen[name] = seen.get(name, 0) + 1
        lines += _variant_section(run, seen[name])
    lines += _provenance(contract)
    lines += _limitations()
    return "\n".join(lines).rstrip("\n")


def write_markdown(
    contract: Contract,
    runs: Sequence[Run],
    path: Path | str,
    *,
    generated_at: str,
) -> Path:
    """Write the Markdown report and return where it landed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(contract, runs, generated_at=generated_at)
    target.write_text(f"{text}\n", encoding=ENCODING)
    return target


def _metadata(contract: Contract, runs: Sequence[Run], generated_at: str) -> list[str]:
    source = contract.source
    return [
        f"- Scenario: `{contract.scenario}`",
        f"- Title: {contract.title}",
        f"- Mode: `{_mode(runs)}`",
        f"- Generated: `{generated_at}`",
        f"- Lab commit: `{source.commit}`",
        f"- Fork commit: `{source.fork_commit}`",
        "",
    ]


def _metrics_table(metrics: Mapping[str, Any]) -> list[str]:
    detection = metrics["fault_detection"]
    weak = metrics["weak_checks"]
    remediation = metrics["remediation"]
    runs = remediation["runs"]
    denominator = f"{runs} remediation run" if runs == 1 else f"{runs} remediation runs"
    rows = ["| Metric | Value | Denominator |", "|---|---|---|"]
    if detection is None:
        rows.append("| Fault detection | not measured | no baseline |")
    else:
        rows.append(
            f"| Fault detection | {detection['failed']} failed | {detection['checks']} checks |"
        )
    if weak is None:
        rows.append("| Weak checks | not measured | no baseline |")
    else:
        rows.append(f"| Weak checks | {weak['passed']} passed | {weak['checks']} checks |")
    for label, key in (
        ("Verified", "verified"),
        ("Rejected", "rejected"),
        ("Inconclusive", "inconclusive"),
        ("Execution errors", "execution_error"),
    ):
        rows.append(f"| {label} | {remediation[key]} | {denominator} |")
    rows.append("")
    return rows


def _variants_table(runs: Sequence[Run]) -> list[str]:
    if not runs:
        return ["No runs in this report.", ""]
    rows = [
        "| Variant | Kind | Mode | Status | Reproduction | Passed |",
        "|---|---|---|---|---|---|",
    ]
    for run in runs:
        verdict = run.verdict
        passed = sum(1 for result in verdict.results if result.status == PASS)
        status = verdict.status or "not applicable"
        rows.append(
            f"| {_cell(verdict.variant)} | {_cell(verdict.kind)} | {_cell(run.mode)} "
            f"| {_cell(status)} | {_cell(verdict.reproduction_status)} "
            f"| {passed}/{len(verdict.results)} |"
        )
    rows.append("")
    return rows


def _variant_section(run: Run, ordinal: int) -> list[str]:
    verdict = run.verdict
    heading = f"### {verdict.variant}" if ordinal == 1 else f"### {verdict.variant} (run {ordinal})"
    status = verdict.status or "not applicable"
    lines = [
        heading,
        "",
        f"- Status: `{status}`",
        f"- Reproduction status: `{verdict.reproduction_status}`",
        f"- Mode: `{run.mode}`",
        f"- Captured: `{run.captured_at}`",
        f"- Reason: {verdict.reason}",
    ]
    if verdict.weak_checks:
        weak = ", ".join(f"`{check_id}`" for check_id in verdict.weak_checks)
        lines.append(f"- Weak checks: {weak}")
    lines.append("")
    if verdict.unresolved:
        lines += ["Unresolved elements:", ""]
        for element in verdict.unresolved:
            lines.append(f"- `{element.id}` from `{element.source}`: {element.reason}")
        lines.append("")
    lines += ["| Check | Fault | Status | Reason | Source |", "|---|---|---|---|---|"]
    for result in verdict.results:
        lines.append(
            f"| {_cell(result.id)} | {_cell(result.fault)} | {_cell(result.status)} "
            f"| {_cell(result.reason)} | {_cell(result.source)} |"
        )
    lines.append("")
    for result in verdict.results:
        lines += [
            f"<details><summary>Evidence for {result.id}</summary>",
            "",
            "```json",
            json.dumps(dict(result.evidence), indent=2, sort_keys=True),
            "```",
            "",
            "</details>",
            "",
        ]
    return lines


def _provenance(contract: Contract) -> list[str]:
    source = contract.source
    lines = [
        "## Provenance",
        "",
        f"- Contract commit: `{source.commit}`",
        f"- Fork commit: `{source.fork_commit}`",
        "",
        "| Check | Source |",
        "|---|---|",
    ]
    for fault in contract.oracle.faults.values():
        for check in fault.checks:
            lines.append(f"| `{check.id}` | `{check.source}` |")
    lines.append("")
    return lines


def _limitations() -> list[str]:
    return [
        "## Limitations",
        "",
        "- This report checks the conditions declared in the incident contract. "
        "It does not prove that no other defect exists.",
        "- A SNAPSHOT run is a recording. It does not call Kubernetes and does not "
        "prove the live cluster is healthy.",
        "- The verdict is advisory and never blocks a merge.",
        "- Only the faults declared in the contract are covered.",
        "",
    ]


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")

"""Decide every variant from snapshot files, with no cluster.

SNAPSHOT reuses `checks`, `decision` and `report` unchanged; only the source of
the observation differs. A fixture is a document a LIVE capture wrote, so its
`meta.provenance` stays `LIVE` while the verdict this module produces says
`SNAPSHOT` (R1).

One document per variant. The baseline document doubles as the pre-fix state of
the four remediations: after `reset` and `break` the cluster is in that state,
and the baseline fixture is the capture that proves it (D57). A LIVE run keeps
a per-variant pre-fix capture because it breaks once per variant; SNAPSHOT has
no cluster to break, so inventing a second document would add a file without
adding evidence.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from verifier.checks import run_checks
from verifier.decision import (
    EXECUTION_ERROR,
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
    Verdict,
    decide_remediation,
    decide_reproduction,
)
from verifier.model import (
    INCONCLUSIVE,
    PASS,
    Contract,
    ObservationError,
    Observations,
    Variant,
)
from verifier.observations import MODE_SNAPSHOT, from_snapshot
from verifier.report import REPRODUCTION, Run
from verifier.runner import BASELINE_VARIANT, REMEDIATION, Attempt, attempts_to_runs


class SnapshotError(RuntimeError):
    """A fixture is missing, unreadable, or belongs to another variant."""


def fixture_path(directory: Path | str, variant: str) -> Path:
    """Return where the fixture for one variant lives."""
    return Path(directory) / f"{variant}.json"


def variant_names(contract: Contract) -> tuple[str, ...]:
    """Return the baseline, then every remediation in contract order."""
    declared = contract.oracle.variants
    rest = tuple(name for name, variant in declared.items() if variant.kind == REMEDIATION)
    return (BASELINE_VARIANT, *rest)


def load_observations(contract: Contract, directory: Path | str, variant: str) -> Observations:
    """Read one fixture and refuse it when it does not describe the variant."""
    path = fixture_path(directory, variant)
    if not path.is_file():
        raise SnapshotError(f"the variant {variant} has no snapshot: expected {path}")
    try:
        observations = from_snapshot(path)
    except ObservationError as error:
        raise SnapshotError(f"the snapshot {path} cannot be read: {error}") from error
    if observations.meta.variant != variant:
        raise SnapshotError(
            f"the snapshot {path} records the variant {observations.meta.variant!r}, "
            f"not {variant!r}"
        )
    return observations


def verify(contract: Contract, directory: Path | str) -> tuple[Attempt, ...]:
    """Decide every variant from its fixture, in contract order.

    A missing or malformed fixture is a SnapshotError, because the run cannot
    produce the verdict it promises. A complete document that lacks a section
    is a different case: it loads with `missing_metadata`, every check turns
    INCONCLUSIVE, and the verdict is INCONCLUSIVE (R11).
    """
    baseline = load_observations(contract, directory, BASELINE_VARIANT)
    baseline_variant = contract.oracle.variants[BASELINE_VARIANT]
    baseline_verdict = decide_reproduction(
        contract, baseline_variant, run_checks(contract, baseline, baseline)
    )
    attempts = [
        _attempt(baseline_variant, baseline_verdict, baseline, None),
    ]
    for name in variant_names(contract)[1:]:
        variant = contract.oracle.variants[name]
        observations = load_observations(contract, directory, name)
        results = run_checks(contract, observations, observations)
        verdict = decide_remediation(contract, variant, results, baseline=baseline_verdict)
        attempts.append(_attempt(variant, verdict, observations, baseline))
    return tuple(attempts)


def runs_of(attempts: Sequence[Attempt]) -> tuple[Run, ...]:
    """Project the attempts onto the report rows, one per variant."""
    return attempts_to_runs(attempts)


def render_table(runs: Sequence[Run]) -> str:
    """Return the five-variant table the review path prints, without colour."""
    rows = [("variant", "mode", "verdict", "reproduction", "checks", "failing")]
    for run in runs:
        verdict = run.verdict
        passed = sum(1 for result in verdict.results if result.status == PASS)
        failing = [result.id for result in verdict.results if result.status != PASS]
        rows.append(
            (
                verdict.variant,
                run.mode,
                verdict.status or "-",
                verdict.reproduction_status or "-",
                f"{passed}/{len(verdict.results)} pass",
                ", ".join(failing) or "-",
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    lines = []
    for position, row in enumerate(rows):
        lines.append(
            "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        )
        if position == 0:
            lines.append("  ".join("-" * width for width in widths))
    lines.append("")
    lines.append(_summary(runs))
    return "\n".join(lines)


def _attempt(
    variant: Variant,
    verdict: Verdict,
    observations: Observations,
    pre_fix: Observations | None,
) -> Attempt:
    return Attempt(
        variant=variant.name,
        mode=MODE_SNAPSHOT,
        verdict=verdict,
        pre_fix=pre_fix,
        start=observations,
        end=observations,
        timing={},
    )


def _summary(runs: Sequence[Run]) -> str:
    counts = Counter(run.verdict.status for run in runs if run.verdict.kind != REPRODUCTION)
    total = sum(counts.values())
    runs_word = "run" if total == 1 else "runs"
    line = (
        f"remediations: {total} {runs_word}, "
        f"{counts[VERIFIED_WITHIN_CONTRACT]} verified, "
        f"{counts[REJECTED]} rejected, "
        f"{counts[INCONCLUSIVE]} inconclusive, "
        f"{counts[EXECUTION_ERROR]} execution error"
    )
    baseline = next((run for run in runs if run.verdict.kind == REPRODUCTION), None)
    if baseline is None:
        return f"baseline: absent\n{line}"
    return f"baseline: {baseline.verdict.reproduction_status or 'UNMEASURED'}\n{line}"

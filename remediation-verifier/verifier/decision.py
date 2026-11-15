"""The verdict for one run. Advisory, never a merge gate.

The decision layer turns check results into a state. It reads nothing else: not
the clock, not the cluster, not the environment, so LIVE and SNAPSHOT reach the
same verdict from the same evidence. Provenance belongs to the observations and
is printed by the report; this module never claims a mode.

A reproduction run has no remediation status. It says whether the incident
reproduced, and a baseline that could not be measured is UNMEASURED rather than
NOT_CONFIRMED, because calling an unmeasured run "the fault is absent" is the
confusion the rubric asks the verifier to avoid.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from verifier.checks import weak_check_ids
from verifier.model import (
    FAIL,
    INCONCLUSIVE,
    CheckResult,
    Contract,
    ContractError,
    Variant,
)
from verifier.results import find_declaration

CONFIRMED = "CONFIRMED"
NOT_CONFIRMED = "NOT_CONFIRMED"
UNMEASURED = "UNMEASURED"

VERIFIED_WITHIN_CONTRACT = "VERIFIED_WITHIN_CONTRACT"
REJECTED = "REJECTED"
EXECUTION_ERROR = "EXECUTION_ERROR"


@dataclass(frozen=True)
class UnresolvedElement:
    """One check the run could not resolve, with the contract line it cites."""

    id: str
    source: str
    reason: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """One run's state, with every check result that produced it."""

    variant: str
    kind: str
    status: str | None
    reproduction_status: str
    reason: str
    results: tuple[CheckResult, ...]
    weak_checks: tuple[str, ...]
    unresolved: tuple[UnresolvedElement, ...]


def decide_reproduction(
    contract: Contract, variant: Variant, results: Sequence[CheckResult]
) -> Verdict:
    """Say whether the baseline reproduced the incident, and nothing else."""
    _require_variant(contract, variant)
    results = tuple(results)
    unresolved = unresolved_elements(contract, results)
    count = len(expected_check_ids(contract))
    problems = []
    coverage = _coverage_reason(contract, results)
    if coverage:
        problems.append(coverage)
    if unresolved:
        problems.append(f"{len(unresolved)} of {count} declared checks did not resolve")
    if problems:
        return Verdict(
            variant=variant.name,
            kind=variant.kind,
            status=None,
            reproduction_status=UNMEASURED,
            reason="; ".join(problems)
            + ", so the baseline neither confirms nor denies the incident",
            results=results,
            weak_checks=(),
            unresolved=unresolved,
        )
    passed = weak_check_ids(results)
    if passed:
        return Verdict(
            variant=variant.name,
            kind=variant.kind,
            status=None,
            reproduction_status=NOT_CONFIRMED,
            reason=f"the baseline did not fail every check: {', '.join(passed)} passed",
            results=results,
            weak_checks=passed,
            unresolved=(),
        )
    return Verdict(
        variant=variant.name,
        kind=variant.kind,
        status=None,
        reproduction_status=CONFIRMED,
        reason=(
            f"the baseline failed all {len(results)} declared checks "
            f"for {', '.join(contract.oracle.expected_faults)}"
        ),
        results=results,
        weak_checks=(),
        unresolved=(),
    )


def decide_remediation(
    contract: Contract,
    variant: Variant,
    results: Sequence[CheckResult],
    *,
    baseline: Verdict,
) -> Verdict:
    """Decide a remediation, never positive without a confirmed pre-fix state."""
    _require_variant(contract, variant)
    results = tuple(results)
    unresolved = unresolved_elements(contract, results)
    if baseline.reproduction_status != CONFIRMED:
        return _verdict(
            variant,
            results,
            INCONCLUSIVE,
            baseline.reproduction_status,
            (
                f"the pre-fix state was {baseline.reproduction_status}, "
                "so no remediation can be positive"
            ),
            unresolved,
        )
    failed = tuple(result.id for result in results if result.status == FAIL)
    if failed:
        return _verdict(
            variant,
            results,
            REJECTED,
            baseline.reproduction_status,
            f"the fix left {len(failed)} checks failing: {', '.join(failed)}",
            unresolved,
        )
    if unresolved:
        count = len(expected_check_ids(contract))
        coverage = _coverage_reason(contract, results)
        problems = [coverage] if coverage else []
        problems.append(f"{len(unresolved)} of {count} declared checks did not resolve")
        return _verdict(
            variant,
            results,
            INCONCLUSIVE,
            baseline.reproduction_status,
            "; ".join(problems),
            unresolved,
        )
    return _verdict(
        variant,
        results,
        VERIFIED_WITHIN_CONTRACT,
        baseline.reproduction_status,
        "all declared checks passed with evidence, within the contract",
        (),
    )


def execution_error(
    contract: Contract, variant: Variant, reason: str, *, baseline: Verdict | None = None
) -> Verdict:
    """Report a run that failed before it produced usable evidence."""
    _require_variant(contract, variant)
    return Verdict(
        variant=variant.name,
        kind=variant.kind,
        status=EXECUTION_ERROR,
        reproduction_status=UNMEASURED if baseline is None else baseline.reproduction_status,
        reason=reason,
        results=(),
        weak_checks=(),
        unresolved=(),
    )


def expected_check_ids(contract: Contract) -> tuple[str, ...]:
    """Return the check ids the contract declares, in contract order."""
    return tuple(
        check.id
        for fault_name in contract.oracle.expected_faults
        for check in contract.oracle.faults[fault_name].checks
    )


def unresolved_elements(
    contract: Contract, results: Sequence[CheckResult]
) -> tuple[UnresolvedElement, ...]:
    """List the declared checks that did not resolve, with their sources."""
    by_id = {result.id: result for result in results}
    declared = expected_check_ids(contract)
    elements = []
    for check_id in declared:
        result = by_id.get(check_id)
        if result is None:
            _, check = find_declaration(contract, check_id)
            elements.append(
                UnresolvedElement(
                    id=check_id,
                    source=check.source,
                    reason="the check did not run",
                )
            )
        elif result.status == INCONCLUSIVE:
            elements.append(
                UnresolvedElement(
                    id=result.id,
                    source=result.source,
                    reason=result.reason,
                    missing=tuple(result.evidence.get("missing_metadata") or ()),
                )
            )
    for result in results:
        if result.id not in declared:
            elements.append(
                UnresolvedElement(
                    id=result.id,
                    source=result.source,
                    reason="the contract does not declare this check",
                )
            )
    return tuple(elements)


def _coverage_reason(contract: Contract, results: Sequence[CheckResult]) -> str | None:
    """Say whether the run produced exactly the checks the contract declares."""
    expected = expected_check_ids(contract)
    produced = {result.id for result in results}
    if len(results) == len(expected) and produced == set(expected):
        return None
    return f"the contract declares {len(expected)} checks and the run produced {len(results)}"


def _verdict(
    variant: Variant,
    results: tuple[CheckResult, ...],
    status: str,
    reproduction_status: str,
    reason: str,
    unresolved: tuple[UnresolvedElement, ...],
) -> Verdict:
    return Verdict(
        variant=variant.name,
        kind=variant.kind,
        status=status,
        reproduction_status=reproduction_status,
        reason=reason,
        results=results,
        weak_checks=(),
        unresolved=unresolved,
    )


def _require_variant(contract: Contract, variant: Variant) -> None:
    declared = contract.oracle.variants.get(variant.name)
    if declared is None:
        raise ContractError(f"the contract declares no variant {variant.name}")
    if declared.kind != variant.kind:
        raise ContractError(
            f"the variant {variant.name} is {declared.kind} in the contract, not {variant.kind}"
        )

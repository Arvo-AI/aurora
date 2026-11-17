"""How a check reports its outcome.

A `CheckResult` is built only from a contract declaration, so its `id`,
`fault` and `source` always match the contract instead of being retyped by
each check. The evidence builders flatten an observation into plain data,
which is what the report prints and what a reviewer reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from verifier.model import (
    INCONCLUSIVE,
    Check,
    CheckResult,
    Contract,
    ContractError,
    Fault,
    Observations,
    Pod,
    Status,
)

OOMKILLED = "OOMKilled"


def build_result(
    contract: Contract,
    check_id: str,
    status: Status,
    reason: str,
    evidence: Mapping[str, Any],
) -> CheckResult:
    """Build a result whose identity comes from the contract, not from the caller."""
    fault_name, check = find_declaration(contract, check_id)
    return CheckResult(
        id=check.id,
        fault=fault_name,
        status=status,
        reason=reason,
        source=check.source,
        evidence=evidence,
    )


def incomplete_result(
    contract: Contract, check_id: str, observations: Observations, *more: Observations
) -> CheckResult | None:
    """Return INCONCLUSIVE when a capture is incomplete, or None when it is usable."""
    names = list(observations.missing_metadata)
    for other in more:
        names.extend(other.missing_metadata)
    missing = tuple(dict.fromkeys(names))
    if not missing:
        return None
    return build_result(
        contract,
        check_id,
        INCONCLUSIVE,
        f"the capture is missing {', '.join(missing)}",
        {"missing_metadata": list(missing)},
    )


def find_declaration(contract: Contract, check_id: str) -> tuple[str, Check]:
    """Return the fault name and the contract entry for one check."""
    for fault_name, fault in contract.oracle.faults.items():
        for check in fault.checks:
            if check.id == check_id:
                return fault_name, check
    raise ContractError(f"the contract declares no check {check_id}")


def fault_for_check(contract: Contract, check_id: str) -> Fault:
    """Return the fault a check belongs to, with its expected response if any."""
    fault_name, _ = find_declaration(contract, check_id)
    return contract.oracle.faults[fault_name]


def pod_evidence(pods: Sequence[Pod]) -> list[dict[str, Any]]:
    """Flatten the pods a check looked at, so a report can print them."""
    return [
        {
            "name": pod.name,
            "uid": pod.uid,
            "restart_count": pod.restart_count,
            "last_terminated_reason": pod.last_terminated_reason,
            "waiting_reason": pod.waiting_reason,
            "terminating": pod.terminating,
        }
        for pod in pods
    ]


def current_pods(pods: Sequence[Pod]) -> tuple[Pod, ...]:
    """Return the pods that are not on their way out.

    A pod with a deletion timestamp is still listed by `kubectl get pods` while
    it terminates, and that is how a rollout leaves an OOMKilled pod behind a
    healthy one. Counting it would fail a fix that worked, so the window checks
    read this view and the evidence keeps the ignored names.
    """
    return tuple(pod for pod in pods if not pod.terminating)


def restart_counts(pods: Sequence[Pod]) -> dict[str, int]:
    """Map each pod, by uid, to its restart count."""
    return {pod.uid or pod.name: pod.restart_count for pod in pods}


def counts_evidence(counts: Mapping[str, int]) -> list[dict[str, Any]]:
    """Turn a restart count map into sorted, printable rows."""
    return [{"pod": key, "restart_count": value} for key, value in sorted(counts.items())]


def has_oomkilled(pod: Pod) -> bool:
    """True when the pod or any of its containers last terminated with OOMKilled."""
    if pod.last_terminated_reason == OOMKILLED:
        return True
    return any(container.last_terminated_reason == OOMKILLED for container in pod.containers)

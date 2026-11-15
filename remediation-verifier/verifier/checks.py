"""The six independent checks.

A check reads the contract and a capture, never the cluster, the clock or the
network, so LIVE and SNAPSHOT reach the same verdict from the same observation.
Each check returns its own `CheckResult` with the raw evidence that supports
it.

The completeness gate is uniform. A capture with `missing_metadata` cannot
support a positive, so every check returns INCONCLUSIVE instead of guessing
(R11). A check that passes at baseline is a weak check and is reported by
`weak_check_ids` (C1), not hidden here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from verifier.model import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    CheckResult,
    Contract,
    ContractError,
    HttpProbe,
    Observations,
)
from verifier.results import (
    build_result,
    counts_evidence,
    fault_for_check,
    find_declaration,
    has_oomkilled,
    incomplete_result,
    pod_evidence,
    restart_counts,
)


def check_no_oomkilled_in_window(
    contract: Contract, start: Observations, end: Observations
) -> CheckResult:
    """Pass when no current worker pod carries an OOMKilled last state."""
    check_id = "no_oomkilled_in_window"
    blocked = incomplete_result(contract, check_id, start, end)
    if blocked is not None:
        return blocked
    if not end.pods:
        return build_result(
            contract,
            check_id,
            INCONCLUSIVE,
            "the end capture has no worker pod to examine",
            {"pods_at_end": 0},
        )
    oomkilled_at_end = [pod.name for pod in end.pods if has_oomkilled(pod)]
    evidence = {
        "pods_at_start": pod_evidence(start.pods),
        "pods_at_end": pod_evidence(end.pods),
        "oomkilled_at_start": [pod.name for pod in start.pods if has_oomkilled(pod)],
        "oomkilled_at_end": oomkilled_at_end,
        "window_seconds": contract.oracle.observation.window_seconds,
    }
    if oomkilled_at_end:
        reason = f"OOMKilled on the current worker pod: {', '.join(oomkilled_at_end)}"
        return build_result(contract, check_id, FAIL, reason, evidence)
    return build_result(
        contract,
        check_id,
        PASS,
        "no current worker pod carries an OOMKilled last state",
        evidence,
    )


def check_restart_count_not_increasing(
    contract: Contract, start: Observations, end: Observations
) -> CheckResult:
    """Pass when the same pod starts and ends the window with zero restarts."""
    check_id = "restart_count_not_increasing"
    blocked = incomplete_result(contract, check_id, start, end)
    if blocked is not None:
        return blocked
    if not start.pods or not end.pods:
        return build_result(
            contract,
            check_id,
            INCONCLUSIVE,
            "a capture has no worker pod, so a restart count cannot be compared",
            {"pods_at_start": len(start.pods), "pods_at_end": len(end.pods)},
        )
    start_counts = restart_counts(start.pods)
    end_counts = restart_counts(end.pods)
    evidence = {
        "start": counts_evidence(start_counts),
        "end": counts_evidence(end_counts),
        "replaced_uids": sorted(set(start_counts) - set(end_counts)),
        "new_uids": sorted(set(end_counts) - set(start_counts)),
        "window_seconds": contract.oracle.observation.window_seconds,
    }
    problems = []
    if set(start_counts) != set(end_counts):
        problems.append("the worker pod was replaced during the window")
    if any(start_counts.values()):
        problems.append("the window did not start from a pod with zero restarts")
    if any(end_counts.values()):
        problems.append("the worker restarted during the window")
    if problems:
        return build_result(contract, check_id, FAIL, "; ".join(problems), evidence)
    return build_result(
        contract,
        check_id,
        PASS,
        "the same pod started and ended the window with zero restarts",
        evidence,
    )


def check_processing_evidence(contract: Contract, observations: Observations) -> CheckResult:
    """Pass when the worker logged enough batches at the captured BATCH_SIZE."""
    check_id = "processing_evidence"
    blocked = incomplete_result(contract, check_id, observations)
    if blocked is not None:
        return blocked
    _, check = find_declaration(contract, check_id)
    if not check.pattern:
        raise ContractError(f"the contract check {check_id} declares no pattern")
    batch_size = observations.batch_size
    if batch_size is None:
        return build_result(
            contract,
            check_id,
            INCONCLUSIVE,
            "the capture does not include the worker batch size",
            {"batch_size": None},
        )
    pattern = check.pattern.format(batch_size=batch_size)
    required = check.min_occurrences or 1
    lines = observations.logs.worker
    matches = [line for line in lines if pattern in line]
    evidence = {
        "batch_size": batch_size,
        "pattern": pattern,
        "required": required,
        "lines_seen": len(lines),
        "match_count": len(matches),
        "matched_lines": matches[:5],
    }
    if len(matches) >= required:
        return build_result(
            contract,
            check_id,
            PASS,
            f"{len(matches)} lines match the current batch size {batch_size}",
            evidence,
        )
    return build_result(
        contract,
        check_id,
        FAIL,
        f"expected {required} lines matching {pattern!r}, found {len(matches)}",
        evidence,
    )


def check_service_has_endpoints(contract: Contract, observations: Observations) -> CheckResult:
    """Pass when the frontend EndpointSlice carries at least one address."""
    check_id = "service_has_endpoints"
    blocked = incomplete_result(contract, check_id, observations)
    if blocked is not None:
        return blocked
    addresses = [address for slice_ in observations.endpoints for address in slice_.addresses]
    evidence = {
        "slices": [
            {"name": slice_.name, "address_count": len(slice_.addresses)}
            for slice_ in observations.endpoints
        ],
        "address_count": len(addresses),
    }
    if addresses:
        return build_result(
            contract,
            check_id,
            PASS,
            f"the frontend EndpointSlice carries {len(addresses)} addresses",
            evidence,
        )
    return build_result(
        contract,
        check_id,
        FAIL,
        "the frontend EndpointSlice carries no address",
        evidence,
    )


def check_endpoints_ready(contract: Contract, observations: Observations) -> CheckResult:
    """Pass when at least one endpoint address reports ready."""
    check_id = "endpoints_ready"
    blocked = incomplete_result(contract, check_id, observations)
    if blocked is not None:
        return blocked
    addresses = [
        {"slice": slice_.name, "address": address.address, "ready": address.ready}
        for slice_ in observations.endpoints
        for address in slice_.addresses
    ]
    ready = [address for address in addresses if address["ready"]]
    evidence = {
        "addresses": addresses,
        "address_count": len(addresses),
        "ready_count": len(ready),
    }
    if ready:
        return build_result(
            contract,
            check_id,
            PASS,
            f"{len(ready)} of {len(addresses)} addresses are ready",
            evidence,
        )
    return build_result(contract, check_id, FAIL, "no endpoint address is ready", evidence)


def check_http_expected_response(contract: Contract, observations: Observations) -> CheckResult:
    """Pass when every captured request returns the expected status and payload."""
    check_id = "http_expected_response"
    blocked = incomplete_result(contract, check_id, observations)
    if blocked is not None:
        return blocked
    fault = fault_for_check(contract, check_id)
    expected_status = fault.expected_status
    expected_json = fault.expected_json or {}
    required = fault.requests or 1
    probes = observations.http
    evidence = {
        "route": fault.http_route,
        "expected_status": expected_status,
        "expected_json": dict(expected_json),
        "requests": [
            {
                "status": probe.status,
                "error": probe.error,
                "items": _items_of(probe.body),
                "duration_seconds": probe.duration_seconds,
            }
            for probe in probes
        ],
    }
    if len(probes) < required:
        return build_result(
            contract,
            check_id,
            INCONCLUSIVE,
            f"{len(probes)} of {required} requests were captured",
            evidence,
        )
    failures = _http_failures(probes, expected_status, expected_json)
    if failures:
        return build_result(contract, check_id, FAIL, "; ".join(failures), evidence)
    return build_result(
        contract,
        check_id,
        PASS,
        f"{len(probes)} requests returned {expected_status} with the expected payload",
        evidence,
    )


def run_checks(
    contract: Contract, start: Observations, end: Observations
) -> tuple[CheckResult, ...]:
    """Run the six checks in contract order and return every result."""
    results = []
    for fault_name in contract.oracle.expected_faults:
        for check in contract.oracle.faults[fault_name].checks:
            results.append(_dispatch(contract, check.id, start, end))
    return tuple(results)


def weak_check_ids(results: Sequence[CheckResult]) -> tuple[str, ...]:
    """Return the ids that passed. A pass on the baseline is a weak check (C1)."""
    return tuple(result.id for result in results if result.status == PASS)


def _dispatch(
    contract: Contract, check_id: str, start: Observations, end: Observations
) -> CheckResult:
    window = _WINDOW_CHECKS.get(check_id)
    if window is not None:
        return window(contract, start, end)
    single = _SINGLE_CHECKS.get(check_id)
    if single is not None:
        return single(contract, end)
    return build_result(
        contract,
        check_id,
        INCONCLUSIVE,
        f"the verifier has no implementation for check {check_id}",
        {},
    )


def _http_failures(
    probes: Sequence[HttpProbe], expected_status: int | None, expected_json: Mapping[str, Any]
) -> list[str]:
    failures = []
    for index, probe in enumerate(probes, start=1):
        if probe.status is None:
            failures.append(f"request {index} did not answer: {probe.error or 'no status'}")
            continue
        if probe.status != expected_status:
            failures.append(f"request {index} returned {probe.status}, expected {expected_status}")
            continue
        body = _json_body(probe.body)
        if body is None:
            failures.append(f"request {index} did not return a JSON object")
            continue
        mismatched = [key for key, value in expected_json.items() if body.get(key) != value]
        if mismatched:
            failures.append(f"request {index} returned an unexpected {', '.join(mismatched)}")
    return failures


def _json_body(body: str | None) -> Mapping[str, Any] | None:
    if body is None:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _items_of(body: str | None) -> Any:
    parsed = _json_body(body)
    return None if parsed is None else parsed.get("items")


_WINDOW_CHECKS: Mapping[str, Callable[[Contract, Observations, Observations], CheckResult]] = {
    "no_oomkilled_in_window": check_no_oomkilled_in_window,
    "restart_count_not_increasing": check_restart_count_not_increasing,
}

_SINGLE_CHECKS: Mapping[str, Callable[[Contract, Observations], CheckResult]] = {
    "processing_evidence": check_processing_evidence,
    "service_has_endpoints": check_service_has_endpoints,
    "endpoints_ready": check_endpoints_ready,
    "http_expected_response": check_http_expected_response,
}

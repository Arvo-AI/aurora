"""What the runner decides when the cluster behaves in a specific way.

The simulated cluster answers kubectl and changes state when a fix is applied,
so the real collector, the real six checks and the real decision layer run
here. Each scenario is a failure mode a reviewer would ask about, including the
two traps: an OOMKilled pod that is still terminating behind a healthy one, and
an address that joins the slice before it is ready.
"""

from __future__ import annotations

import pytest

from simulated_session import simulate
from verifier.checks import run_checks
from verifier.decision import (
    CONFIRMED,
    EXECUTION_ERROR,
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
)
from verifier.model import FAIL, PASS, CheckResult

WORKER_CHECKS = (
    "no_oomkilled_in_window",
    "restart_count_not_increasing",
    "processing_evidence",
)
FRONTEND_CHECKS = ("service_has_endpoints", "endpoints_ready", "http_expected_response")
ALL_CHECKS = WORKER_CHECKS + FRONTEND_CHECKS

CASES = (
    ("full", "full", VERIFIED_WITHIN_CONTRACT, ()),
    ("partial_worker_only", "partial_worker_only", REJECTED, FRONTEND_CHECKS),
    ("partial_frontend_only", "partial_frontend_only", REJECTED, WORKER_CHECKS),
    ("noop", "noop", REJECTED, ALL_CHECKS),
    ("stale_pod", "full", VERIFIED_WITHIN_CONTRACT, ()),
    ("flapping", "full", REJECTED, WORKER_CHECKS),
    ("unready_endpoints", "full", REJECTED, ("endpoints_ready", "http_expected_response")),
    ("no_endpoints", "full", REJECTED, FRONTEND_CHECKS),
)


def by_id(results: tuple[CheckResult, ...]) -> dict[str, CheckResult]:
    return {result.id: result for result in results}


def failing(results: tuple[CheckResult, ...]) -> tuple[str, ...]:
    return tuple(result.id for result in results if result.status == FAIL)


@pytest.mark.parametrize(("scenario", "variant", "status", "failed"), CASES)
def test_the_baseline_confirms_and_the_remediation_reaches_its_state(
    scenario: str, variant: str, status: str, failed: tuple[str, ...]
) -> None:
    simulation = simulate(scenario)
    baseline = simulation.attempt("baseline").verdict
    assert baseline.reproduction_status == CONFIRMED
    assert failing(baseline.results) == ALL_CHECKS

    attempt = simulation.attempt(variant)
    assert attempt.verdict.status == status
    assert failing(attempt.verdict.results) == failed


def test_the_stale_oomkilled_pod_is_evidence_and_not_a_failure() -> None:
    simulation = simulate("stale_pod", variants=["full"])
    attempt = simulation.attempt("full")
    assert attempt.end is not None
    assert {pod.name for pod in attempt.end.pods} == {"worker-w3", "worker-w2"}
    assert [pod.name for pod in attempt.end.pods if pod.terminating] == ["worker-w2"]

    result = by_id(attempt.verdict.results)["no_oomkilled_in_window"]
    assert result.status == PASS
    assert result.evidence["terminating_at_end"] == ["worker-w2"]
    assert "ignored 1 terminating pod" in result.reason


def test_an_address_that_is_not_ready_separates_the_two_endpoint_checks() -> None:
    simulation = simulate("unready_endpoints", variants=["full"])
    results = by_id(simulation.attempt("full").verdict.results)
    assert results["service_has_endpoints"].status == PASS
    assert results["endpoints_ready"].status == FAIL
    assert results["http_expected_response"].status == FAIL


def test_a_restart_inside_the_window_is_a_failure() -> None:
    simulation = simulate("flapping", variants=["full"])
    attempt = simulation.attempt("full")
    assert attempt.start is not None and attempt.end is not None
    assert [pod.restart_count for pod in attempt.start.pods] == [0]
    assert [pod.restart_count for pod in attempt.end.pods] == [2]

    result = by_id(attempt.verdict.results)["restart_count_not_increasing"]
    assert result.status == FAIL
    assert "the worker restarted during the window" in result.reason


def test_the_pre_fix_capture_fails_every_declared_check() -> None:
    simulation = simulate("full", variants=["full"])
    attempt = simulation.attempt("full")
    assert attempt.pre_fix is not None
    results = run_checks(simulation.contract, attempt.pre_fix, attempt.pre_fix)
    assert failing(results) == ALL_CHECKS


def test_a_verified_run_carries_evidence_for_every_check() -> None:
    attempt = simulate("full", variants=["full"]).attempt("full")
    assert attempt.verdict.status == VERIFIED_WITHIN_CONTRACT
    assert all(result.status == PASS for result in attempt.verdict.results)
    assert all(result.evidence for result in attempt.verdict.results)


def test_a_rollout_timeout_is_not_a_seventh_check() -> None:
    """The wait paces the sequence; the end capture and the six checks decide."""
    simulation = simulate("rollout_timeout")
    statuses = {attempt.variant: attempt.verdict.status for attempt in simulation.attempts}
    assert EXECUTION_ERROR not in statuses.values()
    assert statuses["full"] == VERIFIED_WITHIN_CONTRACT
    assert statuses["partial_worker_only"] == REJECTED
    assert statuses["partial_frontend_only"] == REJECTED
    assert statuses["noop"] == REJECTED
    assert all(result.status == PASS for result in simulation.attempt("full").verdict.results)
    assert all(result.evidence for result in simulation.attempt("full").verdict.results)


def test_an_empty_slice_is_rejected_and_not_an_execution_error() -> None:
    simulation = simulate("no_endpoints", variants=["full"])
    verdict = simulation.attempt("full").verdict
    assert verdict.status == REJECTED
    assert failing(verdict.results) == FRONTEND_CHECKS


def test_the_baseline_for_a_scenario_that_does_not_break_is_not_confirmed() -> None:
    simulation = simulate("no_fault")
    assert simulation.attempt("baseline").verdict.reproduction_status != CONFIRMED

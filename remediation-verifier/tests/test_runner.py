"""The runner's sequence, and what it does when a variant cannot finish.

These tests use the simulated cluster only as a source of observations; the
assertions are about the order of the calls, the plan the runner applies and
the verdict it produces when a step fails. The cluster's own arithmetic is
exercised in `test_runner_cluster.py`.
"""

from __future__ import annotations

import pytest

from simulated_session import CAPTURED_AT, simulate
from verifier.decision import (
    CONFIRMED,
    EXECUTION_ERROR,
    INCONCLUSIVE,
    NOT_CONFIRMED,
    VERIFIED_WITHIN_CONTRACT,
)
from verifier.model import ContractError
from verifier.runner import NOT_CAPTURED, Plan, Runner, attempts_to_runs

VARIANTS = (
    "baseline",
    "full",
    "p1_raise_limit",
    "partial_worker_only",
    "partial_frontend_only",
    "noop",
)


def test_the_baseline_breaks_then_captures_both_ends_of_the_window() -> None:
    simulation = simulate(variants=["noop"])
    assert simulation.session.calls[:3] == [
        ("break", 720.0),
        ("capture", "baseline"),
        ("capture", "baseline"),
    ]
    assert simulation.sleep.calls[0] == 60.0


def test_a_remediation_fixes_after_the_pre_fix_capture_and_before_the_waits() -> None:
    simulation = simulate(variants=["full"])
    calls = simulation.session.calls
    block = calls[3:8]
    assert block == [
        ("break", 720.0),
        ("capture", "full"),
        ("fix", simulation.contract.oracle.fixes["full"].steps),
        ("rollout", 120.0),
        ("endpoints", (5.0, 1.0)),
    ]
    assert calls[8:10] == [("capture", "full"), ("capture", "full")]


def test_every_variant_starts_from_its_own_break() -> None:
    simulation = simulate()
    breaks = [call for call in simulation.session.calls if call[0] == "break"]
    assert len(breaks) == len(VARIANTS)
    assert simulation.sleep.calls == [60.0] * len(VARIANTS)


def test_the_attempts_keep_the_contract_order_and_one_row_per_variant() -> None:
    simulation = simulate()
    assert [attempt.variant for attempt in simulation.attempts] == list(VARIANTS)


def test_a_baseline_that_does_not_reproduce_blocks_every_positive() -> None:
    simulation = simulate("no_fault")
    baseline = simulation.attempt("baseline")
    assert baseline.verdict.reproduction_status == NOT_CONFIRMED
    for name in VARIANTS[1:]:
        verdict = simulation.attempt(name).verdict
        assert verdict.status == INCONCLUSIVE
        assert verdict.reproduction_status == NOT_CONFIRMED
        assert "the pre-fix state was NOT_CONFIRMED" in verdict.reason


def test_a_failed_rollout_becomes_an_execution_error_and_the_table_continues() -> None:
    simulation = simulate("rollout_timeout")
    assert len(simulation.attempts) == len(VARIANTS)
    assert simulation.attempt("baseline").verdict.reproduction_status == CONFIRMED
    for name in VARIANTS[1:]:
        verdict = simulation.attempt(name).verdict
        assert verdict.status == EXECUTION_ERROR
        assert verdict.results == ()
        assert verdict.reason.startswith("rollout failed:")


def test_a_variant_that_exceeds_its_budget_stops_before_the_next_phase() -> None:
    simulation = simulate(plan=Plan(variant_timeout_s=0.0), variants=["noop"])
    baseline = simulation.attempt("baseline").verdict
    assert baseline.status == EXECUTION_ERROR
    assert "the variant exceeded 0s before capture_start" in baseline.reason


def test_repetitions_run_each_variant_once_per_cycle() -> None:
    simulation = simulate(variants=["full"], repetitions=2)
    assert [attempt.variant for attempt in simulation.attempts] == [
        "baseline",
        "baseline",
        "full",
        "full",
    ]
    assert simulation.sleep.calls == [60.0] * 4


def test_a_repeated_fix_reaches_the_same_verdict_from_a_fresh_reset() -> None:
    simulation = simulate(variants=["full"], repetitions=2)
    verdicts = [attempt.verdict for attempt in simulation.attempts if attempt.variant == "full"]
    assert {verdict.status for verdict in verdicts} == {VERIFIED_WITHIN_CONTRACT}
    first, second = (attempt.start for attempt in simulation.attempts[2:])
    assert first is not None and second is not None
    assert first.pods[0].uid != "worker-w2"


def test_only_the_requested_remediation_runs() -> None:
    simulation = simulate(variants=["noop"])
    assert {attempt.variant for attempt in simulation.attempts} == {"baseline", "noop"}


def test_the_baseline_cannot_be_requested_as_a_remediation() -> None:
    contract = simulate().contract
    with pytest.raises(ContractError, match="baseline"):
        Runner(contract, simulate(variants=["noop"]).session).run(["baseline"])


def test_an_unknown_variant_is_a_contract_error() -> None:
    contract = simulate().contract
    with pytest.raises(ContractError, match="declares no variant"):
        Runner(contract, simulate(variants=["noop"]).session).run(["ghost"])


def test_a_timing_row_is_recorded_for_every_phase_of_a_remediation() -> None:
    simulation = simulate(variants=["full"])
    timing = simulation.attempt("full").timing
    assert set(timing) == {
        "break",
        "pre_fix",
        "pre_fix_checks",
        "fix",
        "rollout",
        "endpoints",
        "capture_start",
        "window",
        "capture_end",
        "checks",
    }
    assert all(value == 1.0 for value in timing.values())


def test_attempts_project_onto_the_report_runs() -> None:
    simulation = simulate(variants=["full"])
    runs = attempts_to_runs(simulation.attempts)
    assert [run.verdict.variant for run in runs] == ["baseline", "full"]
    assert {run.mode for run in runs} == {"SIMULATED"}
    assert {run.captured_at for run in runs} == {CAPTURED_AT}
    assert runs[1].timing["window"] == 1.0


def test_a_simulated_capture_never_carries_the_live_label() -> None:
    attempt = simulate(variants=["full"]).attempt("full")
    assert attempt.end is not None
    assert attempt.end.meta.mode == "SIMULATED"
    assert attempt.end.meta.provenance == "SIMULATED"


def test_a_variant_that_never_captured_has_no_capture_date() -> None:
    simulation = simulate("rollout_timeout", variants=["noop"])
    assert simulation.attempt("noop").captured_at == NOT_CAPTURED

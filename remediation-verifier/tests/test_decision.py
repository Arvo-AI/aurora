"""Black-box tests for the decision layer of the scenario 12 contract.

Each case starts from check results built through `build_result`, so the id,
the fault and the source of every result come from the contract file. The
tests then assert the verdict the contract requires for that combination,
without looking at how the decision is implemented.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest

from verifier.contract import load_contract
from verifier.decision import (
    CONFIRMED,
    EXECUTION_ERROR,
    NOT_CONFIRMED,
    REJECTED,
    UNMEASURED,
    VERIFIED_WITHIN_CONTRACT,
    UnresolvedElement,
    Verdict,
    decide_remediation,
    decide_reproduction,
    execution_error,
)
from verifier.model import FAIL, INCONCLUSIVE, PASS, CheckResult, Contract, Status, Variant
from verifier.results import build_result

CHECK_ORDER = (
    "no_oomkilled_in_window",
    "restart_count_not_increasing",
    "processing_evidence",
    "service_has_endpoints",
    "endpoints_ready",
    "http_expected_response",
)

ALL_FAIL: Mapping[str, Status] = {check_id: FAIL for check_id in CHECK_ORDER}
ALL_PASS: Mapping[str, Status] = {check_id: PASS for check_id in CHECK_ORDER}
ONE_PASS: Mapping[str, Status] = {**ALL_FAIL, "processing_evidence": PASS}
ONE_GAP: Mapping[str, Status] = {**ALL_FAIL, "endpoints_ready": INCONCLUSIVE}
THREE_AND_THREE: Mapping[str, Status] = {
    "no_oomkilled_in_window": PASS,
    "restart_count_not_increasing": PASS,
    "processing_evidence": PASS,
    "service_has_endpoints": FAIL,
    "endpoints_ready": FAIL,
    "http_expected_response": FAIL,
}
FIVE_PASS_ONE_GAP: Mapping[str, Status] = {**ALL_PASS, "endpoints_ready": INCONCLUSIVE}
FAIL_AND_GAP: Mapping[str, Status] = {
    **ALL_PASS,
    "processing_evidence": FAIL,
    "http_expected_response": INCONCLUSIVE,
}

MISSING_METADATA = ("endpoints",)
GAP_EVIDENCE: Mapping[str, Mapping[str, Any]] = {
    "endpoints_ready": {"missing_metadata": list(MISSING_METADATA)},
}

REMEDIATION_CASES = (
    (ALL_FAIL, ALL_PASS, VERIFIED_WITHIN_CONTRACT),
    (ALL_FAIL, THREE_AND_THREE, REJECTED),
    (ALL_FAIL, FIVE_PASS_ONE_GAP, INCONCLUSIVE),
    (ALL_FAIL, FAIL_AND_GAP, REJECTED),
    (ONE_PASS, ALL_PASS, INCONCLUSIVE),
    (ONE_GAP, ALL_PASS, INCONCLUSIVE),
)

INCONCLUSIVE_CASES = ("endpoints_ready", "processing_evidence")

PROPERTY_CASES = (ALL_FAIL, ALL_PASS, ONE_PASS, ONE_GAP, THREE_AND_THREE)

VERDICT_FIELDS = tuple(entry.name for entry in fields(Verdict))


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract()


@pytest.fixture(scope="module")
def baseline(contract: Contract) -> Variant:
    return contract.oracle.variants["baseline"]


@pytest.fixture(scope="module")
def full(contract: Contract) -> Variant:
    return contract.oracle.variants["full"]


@pytest.fixture(scope="module")
def sources(contract: Contract) -> dict[str, str]:
    return {
        check.id: check.source
        for fault in contract.oracle.faults.values()
        for check in fault.checks
    }


def build_results(
    contract: Contract,
    statuses: Mapping[str, Status] | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[CheckResult, ...]:
    """Build the six results, defaulting every unset check to PASS."""
    chosen = statuses or {}
    extra = evidence or {}
    results = []
    for check_id in CHECK_ORDER:
        status = chosen.get(check_id, PASS)
        results.append(
            build_result(
                contract,
                check_id,
                status,
                f"fixture: {check_id} is {status}",
                extra.get(check_id, {"check": check_id}),
            )
        )
    return tuple(results)


def confirmed_baseline(contract: Contract, baseline: Variant) -> Verdict:
    return decide_reproduction(contract, baseline, build_results(contract, ALL_FAIL))


class _ExplodingTime:
    """Stand-in for the time module or the time function when it is imported."""

    def __call__(self, *args: Any, **kwargs: Any) -> float:
        raise AssertionError("the decision must not read the clock")

    def time(self, *args: Any, **kwargs: Any) -> float:
        raise AssertionError("the decision must not read the clock")


class _ExplodingClock:
    """Stand-in for datetime or the datetime class when it is imported."""

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        raise AssertionError("the decision must not read the clock")

    @classmethod
    def utcnow(cls) -> Any:
        raise AssertionError("the decision must not read the clock")


def _explode(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the decision must not reach the outside world")


class TestReproduction:
    def test_all_six_failures_confirm_the_baseline(self, contract, baseline) -> None:
        verdict = decide_reproduction(contract, baseline, build_results(contract, ALL_FAIL))
        assert verdict.status is None
        assert verdict.reproduction_status == CONFIRMED
        assert verdict.unresolved == ()
        assert verdict.weak_checks == ()
        reason = verdict.reason.lower()
        named = all(fault.lower() in reason for fault in contract.oracle.expected_faults)
        counted = "6" in verdict.reason or "six" in reason
        assert named or counted, verdict.reason

    @pytest.mark.parametrize("check_id", CHECK_ORDER)
    def test_one_passing_check_is_reported_as_weak(self, check_id, contract, baseline) -> None:
        statuses = {**ALL_FAIL, check_id: PASS}
        verdict = decide_reproduction(contract, baseline, build_results(contract, statuses))
        assert verdict.status is None
        assert verdict.reproduction_status == NOT_CONFIRMED
        assert verdict.weak_checks == (check_id,)

    @pytest.mark.parametrize("check_id", INCONCLUSIVE_CASES)
    def test_an_inconclusive_result_leaves_the_baseline_unmeasured(
        self, check_id, contract, baseline, sources
    ) -> None:
        statuses = {**ALL_FAIL, check_id: INCONCLUSIVE}
        evidence = {check_id: {"missing_metadata": list(MISSING_METADATA)}}
        verdict = decide_reproduction(
            contract, baseline, build_results(contract, statuses, evidence)
        )
        assert verdict.status is None
        assert verdict.reproduction_status == UNMEASURED
        assert all(isinstance(element, UnresolvedElement) for element in verdict.unresolved)
        matching = [element for element in verdict.unresolved if element.id == check_id]
        assert matching, "the inconclusive check must be named in unresolved"
        assert matching[0].source == sources[check_id]


class TestRemediation:
    @pytest.mark.parametrize(("pre_fix", "post_fix", "expected"), REMEDIATION_CASES)
    def test_the_verdict_follows_the_results_and_the_pre_fix_state(
        self, pre_fix, post_fix, expected, contract, baseline, full
    ) -> None:
        before = decide_reproduction(contract, baseline, build_results(contract, pre_fix))
        verdict = decide_remediation(
            contract,
            full,
            build_results(contract, post_fix, GAP_EVIDENCE),
            baseline=before,
        )
        assert verdict.status == expected
        assert verdict.reproduction_status == before.reproduction_status

    def test_a_positive_needs_a_confirmed_pre_fix_state(self, contract, baseline, full) -> None:
        before = decide_reproduction(contract, baseline, build_results(contract, ONE_PASS))
        assert before.reproduction_status == NOT_CONFIRMED
        verdict = decide_remediation(
            contract, full, build_results(contract, ALL_PASS), baseline=before
        )
        assert verdict.status == INCONCLUSIVE
        assert verdict.status != VERIFIED_WITHIN_CONTRACT
        assert verdict.reproduction_status == NOT_CONFIRMED
        reason = verdict.reason.lower()
        assert "not confirmed" in reason or "not_confirmed" in reason, verdict.reason

    def test_a_gap_names_the_element_its_source_and_its_missing_metadata(
        self, contract, baseline, full, sources
    ) -> None:
        before = confirmed_baseline(contract, baseline)
        results = build_results(contract, FIVE_PASS_ONE_GAP, GAP_EVIDENCE)
        verdict = decide_remediation(contract, full, results, baseline=before)
        assert verdict.status == INCONCLUSIVE
        assert verdict.status != VERIFIED_WITHIN_CONTRACT
        matching = [element for element in verdict.unresolved if element.id == "endpoints_ready"]
        assert matching, "the inconclusive check must be named in unresolved"
        element = matching[0]
        assert element.source == sources["endpoints_ready"]
        for name in MISSING_METADATA:
            assert name in element.missing

    def test_a_remediation_never_reports_weak_checks(
        self, contract: Contract, baseline: Variant, full: Variant
    ) -> None:
        reproduction = decide_reproduction(contract, baseline, build_results(contract, ALL_FAIL))
        verdict = decide_remediation(
            contract, full, build_results(contract, ALL_PASS), baseline=reproduction
        )
        assert verdict.weak_checks == ()
        assert verdict.status == VERIFIED_WITHIN_CONTRACT

    def test_a_short_result_set_is_never_verified(
        self, contract: Contract, baseline: Variant, full: Variant
    ) -> None:
        before = confirmed_baseline(contract, baseline)
        results = build_results(contract, ALL_PASS)[:5]
        verdict = decide_remediation(contract, full, results, baseline=before)
        assert verdict.status == INCONCLUSIVE
        assert verdict.status != VERIFIED_WITHIN_CONTRACT
        reason = verdict.reason.lower()
        assert "6" in verdict.reason or "six" in reason, verdict.reason
        assert "5" in verdict.reason or "five" in reason, verdict.reason


class TestExecutionError:
    def test_without_a_baseline_the_run_is_unmeasured(self, contract, full) -> None:
        reason = "kubectl get pods exited with status 1"
        verdict = execution_error(contract, full, reason)
        assert verdict.status == EXECUTION_ERROR
        assert verdict.reproduction_status == UNMEASURED
        assert verdict.reason == reason
        assert verdict.results == ()
        assert verdict.unresolved == ()

    def test_a_confirmed_baseline_is_carried_over(self, contract, baseline, full) -> None:
        before = confirmed_baseline(contract, baseline)
        reason = "the rollout did not finish within the timeout"
        verdict = execution_error(contract, full, reason, baseline=before)
        assert verdict.status == EXECUTION_ERROR
        assert verdict.reproduction_status == CONFIRMED
        assert verdict.reason == reason
        assert verdict.results == ()
        assert verdict.unresolved == ()


class TestProperties:
    @pytest.mark.parametrize("field", VERDICT_FIELDS)
    def test_a_verdict_is_frozen(self, field, contract, baseline) -> None:
        verdict = confirmed_baseline(contract, baseline)
        with pytest.raises(FrozenInstanceError):
            setattr(verdict, field, None)

    @pytest.mark.parametrize("statuses", PROPERTY_CASES)
    def test_weak_checks_hold_exactly_the_passes(self, statuses, contract, baseline) -> None:
        verdict = decide_reproduction(contract, baseline, build_results(contract, statuses))
        passed = {check_id for check_id in CHECK_ORDER if statuses[check_id] == PASS}
        assert set(verdict.weak_checks) == passed
        assert len(verdict.weak_checks) == len(set(verdict.weak_checks))

    def test_the_decision_never_reads_the_clock_or_the_environment(
        self, monkeypatch, contract, baseline
    ) -> None:
        expected = confirmed_baseline(contract, baseline)
        assert expected.reproduction_status == CONFIRMED
        monkeypatch.setattr(subprocess, "run", _explode)
        monkeypatch.setattr("verifier.decision.time", _ExplodingTime(), raising=False)
        monkeypatch.setattr("verifier.decision.datetime", _ExplodingClock, raising=False)
        actual = decide_reproduction(contract, baseline, build_results(contract, ALL_FAIL))
        assert actual == expected
        assert actual.reproduction_status == CONFIRMED

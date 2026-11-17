"""Case 9 controls: negative, positive, weak-check and repeatability.

The four controls are an experimental design, not a demo:

- N1 to N3 are negative controls. A verifier that accepts them is useless.
- P1 is the positive control. A verifier that rejects everything would pass
  the three negatives, so the accepted alternative fix is what proves the
  checks are satisfiable.
- C1 mutates the baseline observation until one check would pass. If the
  verdict stays CONFIRMED, that check is not fault-enforcing. The score is
  the checks with a killing mutation over the six declared checks.
- C2 runs the same fix three times from a reset and requires the same verdict.

The mutations touch the observation, not the code: the object under test here
is the oracle, so a mutant is a baseline document with one fault removed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from verifier.checks import run_checks
from verifier.contract import load_contract
from verifier.decision import (
    CONFIRMED,
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
    Verdict,
    decide_remediation,
    decide_reproduction,
)
from verifier.model import FAIL, PASS, CheckResult
from verifier.observations import from_snapshot
from verifier.snapshots import verify

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "snapshots"
REPEATS = Path(__file__).resolve().parents[1] / "fixtures" / "repeats"
CONTRACT = load_contract()
CHECKS = tuple(check.id for fault in CONTRACT.oracle.faults.values() for check in fault.checks)

Document = dict[str, Any]
Mutator = Callable[[Document], None]


def baseline_document() -> Document:
    """A fresh copy of the committed baseline, which every mutation edits."""
    return copy.deepcopy(json.loads((FIXTURES / "baseline.json").read_text(encoding="utf-8")))


def decide(document: Document) -> tuple[Verdict, tuple[CheckResult, ...]]:
    """Run the real checks and the real decision over one document."""
    observations = from_snapshot(document)
    results = run_checks(CONTRACT, observations, observations)
    variant = CONTRACT.oracle.variants["baseline"]
    return decide_reproduction(CONTRACT, variant, results), results


def clear_oom(document: Document) -> None:
    for pod in document["pods"]:
        pod["last_terminated_reason"] = None
        pod["last_exit_code"] = 0
        pod["waiting_reason"] = None
        pod["phase"] = "Running"
        for container in pod.get("containers", []):
            container["last_terminated_reason"] = None
            container["last_exit_code"] = 0
            container["waiting_reason"] = None


def zero_restarts(document: Document) -> None:
    for pod in document["pods"]:
        pod["restart_count"] = 0
        for container in pod.get("containers", []):
            container["restart_count"] = 0


def add_worker_lines(document: Document) -> None:
    document.setdefault("logs", {})["worker"] = [
        "processed batch of 2500 items",
        "processed batch of 2500 items",
    ]


def first_slice(document: Document) -> Document:
    slices = document.setdefault("endpoints", [])
    if not slices:
        slices.append({"name": "frontend", "addresses": []})
    slice_ = slices[0]
    slice_.setdefault("addresses", [])
    return slice_


def add_unready_address(document: Document) -> None:
    first_slice(document)["addresses"].append({"address": "10.244.0.99", "ready": False})


def add_ready_address(document: Document) -> None:
    first_slice(document)["addresses"].append({"address": "10.244.0.99", "ready": True})


def answer_ok(document: Document) -> None:
    for probe in document["http"]:
        probe["status"] = 200
        probe["error"] = None
        probe["body"] = '{"items": ["kettle", "mug", "teapot"]}'


MUTATIONS: dict[str, Mutator] = {
    "no_oomkilled_in_window": clear_oom,
    "restart_count_not_increasing": zero_restarts,
    "processing_evidence": add_worker_lines,
    "service_has_endpoints": add_unready_address,
    "endpoints_ready": add_ready_address,
    "http_expected_response": answer_ok,
}

NEGATIVE_CONTROLS: dict[str, tuple[str, set[str]]] = {
    "N1": (
        "partial_worker_only",
        {"service_has_endpoints", "endpoints_ready", "http_expected_response"},
    ),
    "N2": (
        "partial_frontend_only",
        {
            "no_oomkilled_in_window",
            "restart_count_not_increasing",
            "processing_evidence",
        },
    ),
    "N3": ("noop", set(CHECKS)),
}


def test_the_unmutated_baseline_reproduces_the_incident() -> None:
    verdict, results = decide(baseline_document())
    assert verdict.reproduction_status == CONFIRMED
    assert verdict.weak_checks == ()
    assert {result.status for result in results} == {FAIL}


def test_every_check_has_a_mutation_that_makes_it_pass() -> None:
    killed: list[str] = []
    for check_id, mutate in MUTATIONS.items():
        document = baseline_document()
        mutate(document)
        verdict, results = decide(document)
        statuses = {result.id: result.status for result in results}
        assert statuses[check_id] == PASS, check_id
        assert verdict.reproduction_status != CONFIRMED, check_id
        assert check_id in verdict.weak_checks, check_id
        killed.append(check_id)
    assert sorted(killed) == sorted(CHECKS)
    assert len(killed) == len(CHECKS) == 6


def test_the_endpoint_mutations_are_nested() -> None:
    document = baseline_document()
    add_ready_address(document)
    verdict, results = decide(document)
    assert {result.id: result.status for result in results}["endpoints_ready"] == PASS
    assert set(verdict.weak_checks) == {"service_has_endpoints", "endpoints_ready"}


def test_an_edit_that_changes_no_evidence_keeps_the_baseline_confirmed() -> None:
    document = baseline_document()
    document.setdefault("logs", {}).setdefault("worker", []).append("worker started")
    verdict, _ = decide(document)
    assert verdict.reproduction_status == CONFIRMED


@pytest.mark.parametrize("control", sorted(NEGATIVE_CONTROLS))
def test_the_negative_control_is_rejected(control: str) -> None:
    variant, failing = NEGATIVE_CONTROLS[control]
    attempts = {attempt.variant: attempt for attempt in verify(CONTRACT, FIXTURES)}
    verdict = attempts[variant].verdict
    assert verdict.status == REJECTED
    assert verdict.reproduction_status == CONFIRMED
    observed = {result.id for result in verdict.results if result.status != PASS}
    assert observed == failing


def test_the_positive_control_is_accepted() -> None:
    attempts = {attempt.variant: attempt for attempt in verify(CONTRACT, FIXTURES)}
    verdict = attempts["p1_raise_limit"].verdict
    assert verdict.status == VERIFIED_WITHIN_CONTRACT
    evidence = {result.id: result.evidence for result in verdict.results}
    assert evidence["processing_evidence"]["batch_size"] == 2500
    assert evidence["processing_evidence"]["match_count"] >= 2


def repeat_document(name: str) -> Document:
    return json.loads((REPEATS / name).read_text(encoding="utf-8"))


def test_the_same_fix_from_a_reset_repeats() -> None:
    baseline, baseline_results = decide(baseline_document())
    reference = {result.id: result.status for result in baseline_results}
    for name in ("baseline-r2.json", "baseline-r3.json"):
        repeated, results = decide(repeat_document(name))
        assert repeated.reproduction_status == CONFIRMED, name
        assert {result.id: result.status for result in results} == reference, name

    variant = CONTRACT.oracle.variants["full"]
    statuses: list[dict[str, str]] = []
    captured: list[str] = []
    for path in (
        FIXTURES / "full.json",
        REPEATS / "full-r2.json",
        REPEATS / "full-r3.json",
    ):
        observations = from_snapshot(path)
        results = run_checks(CONTRACT, observations, observations)
        verdict = decide_remediation(CONTRACT, variant, results, baseline=baseline)
        assert verdict.status == VERIFIED_WITHIN_CONTRACT, path.name
        statuses.append({result.id: result.status for result in verdict.results})
        captured.append(observations.meta.captured_at)
    assert statuses[0] == statuses[1] == statuses[2]
    assert len(set(captured)) == 3


def test_the_control_metrics_carry_their_denominators() -> None:
    attempts = verify(CONTRACT, FIXTURES)
    by_variant = {attempt.variant: attempt for attempt in attempts}
    baseline = by_variant["baseline"].verdict
    failed = sum(1 for result in baseline.results if result.status == FAIL)
    false_acceptance = sum(
        1
        for variant, _ in NEGATIVE_CONTROLS.values()
        if by_variant[variant].verdict.status == VERIFIED_WITHIN_CONTRACT
    )
    false_rejection = sum(
        1
        for attempt in attempts
        if attempt.variant == "p1_raise_limit"
        and attempt.verdict.status != VERIFIED_WITHIN_CONTRACT
    )
    metrics = {
        "fault_detection": (failed, len(baseline.results)),
        "weak_checks": (len(baseline.weak_checks), len(baseline.results)),
        "mutations_killed": (len(MUTATIONS), len(CHECKS)),
        "false_acceptance": (false_acceptance, len(NEGATIVE_CONTROLS)),
        "false_rejection": (false_rejection, 1),
        "repeatability": (3, 3),
    }
    assert metrics == {
        "fault_detection": (6, 6),
        "weak_checks": (0, 6),
        "mutations_killed": (6, 6),
        "false_acceptance": (0, 3),
        "false_rejection": (0, 1),
        "repeatability": (3, 3),
    }

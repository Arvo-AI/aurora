"""Golden reports for the structures a verdict can produce.

Each golden is the document the renderer writes for one verdict, built from the
contract and the decision layer with a fixed `generated_at`, so a change in the
renderer shows up as a diff instead of passing unnoticed. Regenerate after a
deliberate format change:

    UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/test_report_golden.py

Five JSON goldens cover the shapes the document takes: a baseline only, a
verified remediation, a remediation blocked by its pre-fix gate, a remediation
with unresolved checks, and a run that produced no check results.

A state that differs from another only in its words is asserted in
`tests/test_report.py` instead of being repeated here: a golden that is 90
percent a copy of its neighbour pins nothing new.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from verifier.contract import load_contract
from verifier.decision import (
    Verdict,
    decide_remediation,
    decide_reproduction,
    execution_error,
)
from verifier.model import FAIL, INCONCLUSIVE, PASS, CheckResult, Contract, Status
from verifier.report import Run, report_document
from verifier.results import build_result

GENERATED = "2026-11-15T21:05:00Z"
CAPTURED = "2026-11-15T20:00:00Z"
MODE = "SNAPSHOT"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "reports"
TIMEOUT = "kubectl rollout status timed out after 120s"

CHECK_IDS = (
    "no_oomkilled_in_window",
    "restart_count_not_increasing",
    "processing_evidence",
    "service_has_endpoints",
    "endpoints_ready",
    "http_expected_response",
)

ALL_FAIL: Mapping[str, Status] = {check_id: FAIL for check_id in CHECK_IDS}
ALL_PASS: Mapping[str, Status] = {check_id: PASS for check_id in CHECK_IDS}
ONE_PASS: Mapping[str, Status] = {**ALL_FAIL, "endpoints_ready": PASS}
FIVE_PASS_ONE_GAP: Mapping[str, Status] = {**ALL_PASS, "http_expected_response": INCONCLUSIVE}

JSON_STATES = (
    "confirmed",
    "verified",
    "blocked",
    "inconclusive",
    "execution_error",
)


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract()


def results(
    contract: Contract,
    statuses: Mapping[str, Status],
    evidence: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[CheckResult, ...]:
    extra = evidence or {}
    return tuple(
        build_result(
            contract,
            check_id,
            statuses[check_id],
            f"{statuses[check_id]} fixture",
            extra.get(check_id, {"fixture": check_id}),
        )
        for check_id in CHECK_IDS
    )


def reproduction(
    contract: Contract,
    statuses: Mapping[str, Status],
    evidence: Mapping[str, Mapping[str, object]] | None = None,
) -> Verdict:
    return decide_reproduction(
        contract, contract.oracle.variants["baseline"], results(contract, statuses, evidence)
    )


def remediation(
    contract: Contract,
    name: str,
    statuses: Mapping[str, Status],
    baseline: Verdict,
    evidence: Mapping[str, Mapping[str, object]] | None = None,
) -> Verdict:
    return decide_remediation(
        contract,
        contract.oracle.variants[name],
        results(contract, statuses, evidence),
        baseline=baseline,
    )


def runs_for(state: str, contract: Contract) -> list[Run]:
    confirmed = reproduction(contract, ALL_FAIL)
    if state == "confirmed":
        return [Run(confirmed, MODE, CAPTURED)]
    if state == "verified":
        full = remediation(contract, "full", ALL_PASS, confirmed)
        return [Run(confirmed, MODE, CAPTURED), Run(full, MODE, CAPTURED, {"pods": 0.12})]
    if state == "blocked":
        weak = reproduction(contract, ONE_PASS)
        full = remediation(contract, "full", ALL_PASS, weak)
        return [Run(weak, MODE, CAPTURED), Run(full, MODE, CAPTURED)]
    if state == "inconclusive":
        gap = {"http_expected_response": {"missing_metadata": ["http"]}}
        full = remediation(contract, "full", FIVE_PASS_ONE_GAP, confirmed, gap)
        return [Run(confirmed, MODE, CAPTURED), Run(full, MODE, CAPTURED)]
    if state == "execution_error":
        broken = execution_error(
            contract, contract.oracle.variants["full"], TIMEOUT, baseline=confirmed
        )
        return [Run(confirmed, MODE, CAPTURED), Run(broken, MODE, CAPTURED)]
    raise AssertionError(f"unknown state {state}")


def compare(path: Path, actual: str) -> None:
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.is_file(), f"missing {path.name}, regenerate with UPDATE_GOLDEN=1"
    assert path.read_text(encoding="utf-8") == actual, f"{path.name} changed"


@pytest.mark.parametrize("state", JSON_STATES)
def test_the_json_report_matches_its_golden(state: str, contract: Contract) -> None:
    document = report_document(contract, runs_for(state, contract), generated_at=GENERATED)
    actual = json.dumps(document, indent=2, sort_keys=True) + "\n"
    compare(GOLDEN_DIR / f"{state}.json", actual)


def test_the_golden_directory_holds_only_the_expected_files() -> None:
    expected = {f"{state}.json" for state in JSON_STATES}
    assert {path.name for path in GOLDEN_DIR.glob("*")} == expected

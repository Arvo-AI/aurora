"""Black-box tests for the six checks of the scenario 12 contract.

Every case starts from one complete snapshot document, changes only the field
the rule under test reads, and asserts the status the contract requires. The
checks are called through their public names, and the expected identifiers,
fault names and source strings come from the contract file itself.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from verifier.checks import (
    check_endpoints_ready,
    check_http_expected_response,
    check_no_oomkilled_in_window,
    check_processing_evidence,
    check_restart_count_not_increasing,
    check_service_has_endpoints,
    run_checks,
    weak_check_ids,
)
from verifier.contract import load_contract
from verifier.model import FAIL, INCONCLUSIVE, PASS, CheckResult, Contract
from verifier.observations import from_snapshot

GOOD_BODY = '{"items": ["kettle", "mug", "teapot"]}'
BATCH_LINE = "processed batch of 2500 items"

CHECK_ORDER = (
    "no_oomkilled_in_window",
    "restart_count_not_increasing",
    "processing_evidence",
    "service_has_endpoints",
    "endpoints_ready",
    "http_expected_response",
)

GATE_CASES = (
    ("no_oomkilled_in_window", "provenance"),
    ("restart_count_not_increasing", "restart_counts"),
    ("processing_evidence", "logs"),
    ("service_has_endpoints", "endpoints"),
    ("endpoints_ready", "endpoints"),
    ("http_expected_response", "provenance"),
)

WINDOW_CHECKS = {
    "no_oomkilled_in_window": check_no_oomkilled_in_window,
    "restart_count_not_increasing": check_restart_count_not_increasing,
}

SINGLE_CHECKS = {
    "processing_evidence": check_processing_evidence,
    "service_has_endpoints": check_service_has_endpoints,
    "endpoints_ready": check_endpoints_ready,
    "http_expected_response": check_http_expected_response,
}


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract()


def probe(
    status: int | None = 200, body: str | None = GOOD_BODY, error: str | None = None
) -> dict[str, Any]:
    return {
        "route": "http://frontend.shop.svc.cluster.local/api/items",
        "status": status,
        "body": body,
        "error": error,
    }


def pod(uid: str = "u1", restarts: int = 0, reason: str | None = None) -> dict[str, Any]:
    ready = restarts == 0
    container = {
        "name": "worker",
        "ready": ready,
        "restart_count": restarts,
        "last_terminated_reason": reason,
    }
    return {
        "name": "worker-1",
        "uid": uid,
        "ready": ready,
        "restart_count": restarts,
        "last_terminated_reason": reason,
        "containers": [container],
    }


def endpoint_slice(*ready: bool) -> dict[str, Any]:
    addresses = [
        {"address": f"10.244.0.{index + 5}", "ready": flag} for index, flag in enumerate(ready)
    ]
    return {"name": "frontend-abc", "addresses": addresses}


def document(**overrides: Any) -> dict[str, Any]:
    """Return a complete capture in which all six checks pass."""
    data = {
        "meta": {
            "lab_commit": "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0",
            "variant": "full",
            "captured_at": "2026-11-15T20:00:00Z",
            "provenance": "LIVE",
        },
        "pods": [pod()],
        "services": [{"name": "frontend", "selector": {"app": "frontend"}, "ports": [80]}],
        "endpoints": [endpoint_slice(True)],
        "http": [probe(), probe(), probe()],
        "logs": {"worker": [BATCH_LINE, BATCH_LINE], "loadgen": ["GET /api/items -> 200"]},
        "worker": {"batch_size": 2500},
    }
    data.update(overrides)
    return data


def baseline() -> dict[str, Any]:
    """Return the reproduction capture: two faults, all six checks fail."""
    data = document(
        pods=[pod(restarts=4, reason="OOMKilled")],
        endpoints=[endpoint_slice()],
        http=[probe(status=503, body="")] * 3,
        logs={"worker": []},
    )
    data["meta"]["variant"] = "baseline"
    return data


def container_oom() -> dict[str, Any]:
    data = document()
    data["pods"][0]["containers"][0]["last_terminated_reason"] = "OOMKilled"
    return data


def damaged(defect: str) -> dict[str, Any]:
    """Return a capture that lost one piece of the metadata a check reads."""
    data = document()
    if defect == "restart_counts":
        del data["pods"][0]["restart_count"]
        del data["pods"][0]["containers"][0]["restart_count"]
    elif defect == "provenance":
        data["meta"]["provenance"] = "SNAPSHOT"
    else:
        del data[defect]
    return data


def status_of(
    check_id: str, contract: Contract, end: dict[str, Any], start: dict[str, Any] | None = None
) -> str:
    """Run one check over a start and an end capture, defaulting to a clean start."""
    begin = from_snapshot(document() if start is None else start)
    observations = from_snapshot(end)
    if check_id in WINDOW_CHECKS:
        return WINDOW_CHECKS[check_id](contract, begin, observations).status
    return SINGLE_CHECKS[check_id](contract, observations).status


OOM_CASES = (
    (document(), PASS),
    (document(pods=[pod(reason="OOMKilled")]), FAIL),
    (container_oom(), FAIL),
    (document(pods=[]), INCONCLUSIVE),
)

RESTART_CASES = (
    (document(), document(), PASS),
    (document(pods=[pod(restarts=4)]), document(pods=[pod(restarts=4)]), FAIL),
    (document(), document(pods=[pod(restarts=2)]), FAIL),
    (document(), document(pods=[pod(uid="u2")]), FAIL),
    (document(pods=[]), document(), INCONCLUSIVE),
    (document(), document(pods=[]), INCONCLUSIVE),
    (damaged("restart_counts"), document(), INCONCLUSIVE),
)

PROCESSING_CASES = (
    (document(), PASS),
    (document(logs={"worker": [BATCH_LINE]}), FAIL),
    (document(logs={"worker": ["processed batch of 500 items"] * 2}), FAIL),
    (document(logs={"worker": []}), FAIL),
)

ENDPOINT_CASES = (
    ("service_has_endpoints", document(), PASS),
    ("service_has_endpoints", document(endpoints=[endpoint_slice()]), FAIL),
    ("service_has_endpoints", document(endpoints=[]), FAIL),
    ("endpoints_ready", document(), PASS),
    ("endpoints_ready", document(endpoints=[endpoint_slice(False, True)]), PASS),
    ("endpoints_ready", document(endpoints=[endpoint_slice(False, False)]), FAIL),
    ("endpoints_ready", document(endpoints=[]), FAIL),
)

HTTP_CASES = (
    (document(), PASS),
    (document(http=[probe(), probe(), probe(status=503, body="")]), FAIL),
    (document(http=[probe(body='{"items": ["kettle"]}')] * 3), FAIL),
    (document(http=[probe(body="oops")] * 3), FAIL),
    (document(http=[probe(status=None, body=None, error="timeout")] * 3), FAIL),
    (document(http=[probe(), probe()]), INCONCLUSIVE),
    (document(http=[probe(body='{"items": ["kettle", "mug", "teapot"], "count": 3}')] * 3), PASS),
    (document(http=[probe(body='{"count": 3, "items": ["kettle", "mug", "teapot"]}')] * 3), PASS),
)


class TestCompletenessGate:
    @pytest.mark.parametrize(("check_id", "defect"), GATE_CASES)
    def test_missing_metadata_is_inconclusive(self, check_id, defect, contract) -> None:
        data = damaged(defect)
        assert from_snapshot(data).missing_metadata, "the case must remove metadata"
        assert status_of(check_id, contract, data) == INCONCLUSIVE


class TestNoOomkilledInWindow:
    @pytest.mark.parametrize(("end", "expected"), OOM_CASES)
    def test_the_oom_rule_and_its_evidence(self, end, expected, contract) -> None:
        start = document()
        result = check_no_oomkilled_in_window(contract, from_snapshot(start), from_snapshot(end))
        assert result.status == expected
        if expected == FAIL:
            evidence = json.dumps(result.evidence, default=str)
            assert "worker-1" in evidence
            assert "u1" in evidence


class TestRestartCountNotIncreasing:
    @pytest.mark.parametrize(("start", "end", "expected"), RESTART_CASES)
    def test_the_restart_rule(self, start, end, expected, contract) -> None:
        result = check_restart_count_not_increasing(
            contract, from_snapshot(start), from_snapshot(end)
        )
        assert result.status == expected


class TestProcessingEvidence:
    @pytest.mark.parametrize(("data", "expected"), PROCESSING_CASES)
    def test_the_batch_rule(self, data, expected, contract) -> None:
        assert not from_snapshot(data).missing_metadata
        assert status_of("processing_evidence", contract, data) == expected

    def test_a_capture_without_the_worker_section_is_inconclusive(self, contract) -> None:
        data = document()
        del data["worker"]
        assert not from_snapshot(data).batch_size
        assert status_of("processing_evidence", contract, data) == INCONCLUSIVE


class TestEndpoints:
    @pytest.mark.parametrize(("check_id", "data", "expected"), ENDPOINT_CASES)
    def test_the_endpoint_rules(self, check_id, data, expected, contract) -> None:
        assert not from_snapshot(data).missing_metadata
        assert status_of(check_id, contract, data) == expected


class TestHttpExpectedResponse:
    @pytest.mark.parametrize(("data", "expected"), HTTP_CASES)
    def test_the_response_rule(self, data, expected, contract) -> None:
        assert not from_snapshot(data).missing_metadata
        assert status_of("http_expected_response", contract, data) == expected


class TestRunChecks:
    def test_the_six_checks_in_order_with_their_sources(self, contract) -> None:
        expected = {
            check.id: (fault.name, check.source)
            for fault in contract.oracle.faults.values()
            for check in fault.checks
        }
        assert set(expected) == set(CHECK_ORDER)
        observations = from_snapshot(document())
        results = run_checks(contract, observations, observations)
        assert tuple(result.id for result in results) == CHECK_ORDER
        assert {(result.id, result.fault, result.source) for result in results} == {
            (check_id, *meta) for check_id, meta in expected.items()
        }
        assert [result.status for result in results] == [PASS] * 6
        assert weak_check_ids(results) == CHECK_ORDER

    def test_the_baseline_fails_everything_and_marks_nothing_weak(self, contract) -> None:
        observations = from_snapshot(baseline())
        results = run_checks(contract, observations, observations)
        assert [result.status for result in results] == [FAIL] * 6
        assert weak_check_ids(results) == ()

    def test_only_passes_are_reported_as_weak(self) -> None:
        results = tuple(
            CheckResult(
                id=name, fault="worker-oom", status=status, reason="", source="", evidence={}
            )
            for name, status in (("first", PASS), ("second", FAIL), ("third", FAIL))
        )
        assert weak_check_ids(results) == ("first",)

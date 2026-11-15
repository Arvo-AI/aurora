"""Tests for the result layer: how a check's outcome is built and printed."""

from __future__ import annotations

from dataclasses import replace

import pytest

from verifier.contract import load_contract
from verifier.model import (
    INCONCLUSIVE,
    PASS,
    ContainerStatus,
    Contract,
    ContractError,
    Logs,
    Meta,
    Observations,
    Pod,
)
from verifier.results import (
    build_result,
    counts_evidence,
    find_declaration,
    has_oomkilled,
    incomplete_result,
    pod_evidence,
    restart_counts,
)

CHECK_IDS = (
    "no_oomkilled_in_window",
    "restart_count_not_increasing",
    "processing_evidence",
    "service_has_endpoints",
    "endpoints_ready",
    "http_expected_response",
)


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract()


def complete() -> Observations:
    return Observations(
        meta=Meta(
            lab_commit="4edcd9dfbc48",
            variant="baseline",
            mode="SNAPSHOT",
            captured_at="2026-11-15T20:00:00Z",
            provenance="LIVE",
        ),
        pods=(),
        services=(),
        endpoints=(),
        http=(),
        logs=Logs(),
        timing={},
    )


def pod(
    uid: str = "u1",
    name: str = "worker-1",
    restart_count: int = 2,
    reason: str | None = None,
    containers: tuple[ContainerStatus, ...] = (),
) -> Pod:
    return Pod(
        name=name,
        uid=uid,
        phase="Running",
        ready=False,
        restart_count=restart_count,
        last_terminated_reason=reason,
        containers=containers,
    )


@pytest.mark.parametrize("check_id", CHECK_IDS)
def test_the_result_identity_comes_from_the_contract(contract: Contract, check_id: str) -> None:
    result = build_result(contract, check_id, PASS, "a reason", {})
    fault_name, check = find_declaration(contract, check_id)
    assert (result.id, result.fault, result.source) == (check_id, fault_name, check.source)
    assert (result.status, result.reason) == (PASS, "a reason")


def test_each_fault_owns_its_three_checks(contract: Contract) -> None:
    worker = {find_declaration(contract, check_id)[0] for check_id in CHECK_IDS[:3]}
    frontend = {find_declaration(contract, check_id)[0] for check_id in CHECK_IDS[3:]}
    assert worker == {"worker-oom"}
    assert frontend == {"frontend-selector"}


def test_an_unknown_check_is_a_contract_error(contract: Contract) -> None:
    with pytest.raises(ContractError):
        build_result(contract, "not_a_check", PASS, "a reason", {})


def test_an_incomplete_capture_is_named_once_and_in_order(contract: Contract) -> None:
    start = replace(complete(), missing_metadata=("pods",))
    end = replace(complete(), missing_metadata=("pods", "worker_logs"))
    result = incomplete_result(contract, "restart_count_not_increasing", start, end)
    assert result is not None
    assert result.status == INCONCLUSIVE
    assert result.evidence["missing_metadata"] == ["pods", "worker_logs"]
    assert result.id == "restart_count_not_increasing"


def test_a_complete_capture_returns_no_result(contract: Contract) -> None:
    assert incomplete_result(contract, "processing_evidence", complete()) is None


def test_pod_evidence_keeps_the_fields_a_report_prints() -> None:
    evidence = pod_evidence([pod(reason="OOMKilled")])
    assert evidence == [
        {
            "name": "worker-1",
            "uid": "u1",
            "restart_count": 2,
            "last_terminated_reason": "OOMKilled",
            "waiting_reason": None,
        }
    ]


def test_restart_counts_use_the_name_when_the_uid_is_empty() -> None:
    assert restart_counts([pod(uid="", name="worker-9", restart_count=3)]) == {"worker-9": 3}


def test_counts_evidence_is_sorted_by_pod() -> None:
    rows = counts_evidence({"b": 2, "a": 0})
    assert rows == [{"pod": "a", "restart_count": 0}, {"pod": "b", "restart_count": 2}]


def test_oomkilled_is_read_from_the_pod_and_from_a_container() -> None:
    container = ContainerStatus(
        name="worker", ready=False, restart_count=1, last_terminated_reason="OOMKilled"
    )
    assert has_oomkilled(pod(reason="OOMKilled"))
    assert has_oomkilled(pod(containers=(container,)))
    assert not has_oomkilled(pod(reason="Error"))

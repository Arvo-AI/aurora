"""The pods a window check is willing to count.

A rollout can leave an OOMKilled pod in Terminating behind a healthy one, and
`kubectl get pods` keeps listing it. Counting it would fail a fix that worked,
so the capture marks it and the window checks leave it out while the evidence
still names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from verifier.checks import check_no_oomkilled_in_window, check_restart_count_not_increasing
from verifier.contract import load_contract
from verifier.live import normalize_pods
from verifier.model import FAIL, INCONCLUSIVE, PASS, Observations, Pod
from verifier.observations import from_snapshot
from verifier.results import current_pods

CAPTURED_AT = "2026-11-15T20:00:00Z"


def pod(
    name: str,
    *,
    restarts: int = 0,
    reason: str | None = None,
    terminating: bool = False,
    ready: bool = True,
) -> Pod:
    return Pod(
        name=name,
        uid=name,
        phase="Running",
        ready=ready,
        restart_count=restarts,
        last_terminated_reason=reason,
        last_exit_code=137 if reason else None,
        terminating=terminating,
    )


def pod_document(item: Pod) -> dict[str, Any]:
    return {
        "name": item.name,
        "uid": item.uid,
        "phase": item.phase,
        "ready": item.ready,
        "restart_count": item.restart_count,
        "last_terminated_reason": item.last_terminated_reason,
        "last_exit_code": item.last_exit_code,
        "terminating": item.terminating,
    }


def observation(*pods: Pod, batch_size: int = 500) -> Observations:
    return from_snapshot(
        {
            "meta": {
                "lab_commit": "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0",
                "variant": "full",
                "mode": "LIVE",
                "captured_at": CAPTURED_AT,
                "provenance": "LIVE",
            },
            "pods": [pod_document(item) for item in pods],
            "services": [],
            "endpoints": [],
            "http": [],
            "logs": {"worker": ["processed batch of 500 items"], "loadgen": []},
            "worker": {"batch_size": batch_size},
        }
    )


def test_current_pods_drops_the_ones_on_their_way_out() -> None:
    pods = (pod("worker-w3"), pod("worker-w2", reason="OOMKilled", terminating=True))
    assert [item.name for item in current_pods(pods)] == ["worker-w3"]


def test_a_terminating_oomkilled_pod_does_not_fail_the_window_check() -> None:
    contract = load_contract()
    healthy = pod("worker-w3")
    stale = pod("worker-w2", restarts=7, reason="OOMKilled", terminating=True)
    observation_with_stale = observation(healthy, stale)

    result = check_no_oomkilled_in_window(contract, observation_with_stale, observation_with_stale)

    assert result.status == PASS
    assert result.evidence["oomkilled_at_end"] == []
    assert result.evidence["terminating_at_end"] == ["worker-w2"]
    assert "ignored 1 terminating pod" in result.reason


def test_a_current_oomkilled_pod_still_fails_the_window_check() -> None:
    contract = load_contract()
    current = pod("worker-w3", restarts=7, reason="OOMKilled", ready=False)
    captured = observation(current)

    result = check_no_oomkilled_in_window(contract, captured, captured)

    assert result.status == FAIL
    assert result.evidence["oomkilled_at_end"] == ["worker-w3"]


def test_a_terminating_pod_that_disappears_is_not_a_replacement() -> None:
    contract = load_contract()
    healthy = pod("worker-w3")
    stale = pod("worker-w2", restarts=7, reason="OOMKilled", terminating=True)

    result = check_restart_count_not_increasing(
        contract, observation(healthy, stale), observation(healthy)
    )

    assert result.status == PASS
    assert result.evidence["replaced_uids"] == []
    assert result.evidence["terminating_ignored"] == {"start": ["worker-w2"], "end": []}


def test_a_capture_with_only_terminating_pods_cannot_resolve_the_window() -> None:
    contract = load_contract()
    stale = pod("worker-w2", restarts=7, reason="OOMKilled", terminating=True)
    captured = observation(stale)

    assert check_no_oomkilled_in_window(contract, captured, captured).status == INCONCLUSIVE
    assert check_restart_count_not_increasing(contract, captured, captured).status == INCONCLUSIVE


def test_a_snapshot_without_the_field_reads_as_a_current_pod() -> None:
    document: Mapping[str, Any] = {
        "meta": {
            "lab_commit": "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0",
            "variant": "baseline",
            "captured_at": "2026-11-15T20:00:00Z",
            "provenance": "LIVE",
        },
        "pods": [{"name": "worker-w2", "uid": "u2", "phase": "Running", "ready": True}],
        "services": [],
        "endpoints": [],
        "http": [],
        "logs": {"worker": [], "loadgen": []},
        "worker": {"batch_size": 500},
    }
    captured = from_snapshot(document)
    assert captured.pods[0].terminating is False


def test_the_live_normalizer_reads_the_deletion_timestamp() -> None:
    pods = normalize_pods(
        [
            {
                "metadata": {
                    "name": "worker-w2",
                    "uid": "u2",
                    "deletionTimestamp": "2026-11-15T20:00:30Z",
                }
            },
            {"metadata": {"name": "worker-w3", "uid": "u3"}},
        ]
    )
    assert [item["terminating"] for item in pods] == [True, False]

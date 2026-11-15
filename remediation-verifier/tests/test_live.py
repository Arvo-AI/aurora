"""Black-box tests for the live capture path.

Fakes stand in for the cluster, so the tests fix the commands and call counts.
"""

from __future__ import annotations

import copy
import json
from typing import Any, NamedTuple

import pytest

from verifier.contract import load_contract
from verifier.kubectl import LiveError
from verifier.live import from_live, normalize_endpoints
from verifier.model import Observations
from verifier.observations import (
    MODE_LIVE,
    MODE_SNAPSHOT,
    PROVENANCE_LIVE,
    from_snapshot,
    snapshot_document,
)

CAPTURED_AT = "2026-11-15T20:00:00Z"
VARIANT = "baseline"
ROUTE = "http://frontend.shop.svc.cluster.local/api/items"
WORKER_LOG = "processed batch of 2500 items"
LOADGEN_LOG = "GET /api/items -> 503"

PODS_ARGS = ["-n", "shop", "get", "pods", "-l", "app=worker"]
SERVICE_ARGS = ["-n", "shop", "get", "service", "frontend"]
SLICE_ARGS = ["-n", "shop", "get", "endpointslices", "-l", "kubernetes.io/service-name=frontend"]
WORKER_LOG_ARGS = ["-n", "shop", "logs", "deployment/worker", "--since=60s"]
LOADGEN_LOG_ARGS = ["-n", "shop", "logs", "deployment/loadgen", "--since=60s"]

PODS_PAYLOAD = json.loads(
    '{"items": [{"metadata": {"name": "worker-7d9", "uid": "abc"}, "status": {"phase": "Running",'
    ' "containerStatuses": [{"name": "worker", "ready": false, "restartCount": 4,'
    ' "state": {"waiting": {"reason": "CrashLoopBackOff"}},'
    ' "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}]}'
)
SERVICE_PAYLOAD = json.loads(
    '{"metadata": {"name": "frontend"}, "spec": {"selector": {"app": "frontend", "tier": "web"},'
    ' "ports": [{"port": 80}], "clusterIP": "10.96.0.10"}}'
)
ENDPOINTS_PAYLOAD = json.loads(
    '{"items": [{"metadata": {"name": "frontend-two"}, "endpoints": ['
    '{"addresses": ["10.244.0.5"], "conditions": {"ready": false}},'
    '{"addresses": ["10.244.0.6"], "conditions": {"ready": true}}]}]}'
)


class FakeKubectl:
    """Records the commands it receives and replays the documented payloads."""

    def __init__(self) -> None:
        self.get_calls: list[list[str]] = []
        self.text_calls: list[list[str]] = []

    def get(self, args: list[str]) -> dict[str, Any]:
        self.get_calls.append(list(args))
        if "pods" in args:
            return copy.deepcopy(PODS_PAYLOAD)
        if "service" in args:
            return copy.deepcopy(SERVICE_PAYLOAD)
        return copy.deepcopy(ENDPOINTS_PAYLOAD)

    def text(self, args: list[str]) -> str:
        self.text_calls.append(list(args))
        if "deployment/worker" in args:
            return f"{WORKER_LOG}\n{WORKER_LOG}\n"
        return f"{LOADGEN_LOG}\n"


class FailingKubectl(FakeKubectl):
    """A kubectl that reports a command failure on every read."""

    def get(self, args: list[str]) -> dict[str, Any]:
        raise LiveError("kubectl failed")


class FakeProbe:
    """Records the route and the timeout of every HTTP probe."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, route: str, *args: object, **kwargs: object) -> dict[str, Any]:
        self.calls.append((route, args[0] if args else kwargs.get("timeout")))
        return {"route": route, "status": 200, "body": "{}", "error": None}


class FakeSleep:
    """Records the requested waits without waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class Capture(NamedTuple):
    """One live capture with the fakes that produced it."""

    observations: Observations
    kubectl: FakeKubectl
    probe: FakeProbe
    sleep: FakeSleep


@pytest.fixture()
def capture() -> Capture:
    kubectl = FakeKubectl()
    probe = FakeProbe()
    sleep = FakeSleep()
    observations = from_live(
        load_contract(),
        VARIANT,
        kubectl=kubectl,
        probe=probe,
        sleep=sleep,
        captured_at=CAPTURED_AT,
    )
    return Capture(observations, kubectl, probe, sleep)


def test_the_capture_is_complete(capture: Capture) -> None:
    assert capture.observations.missing_metadata == ()
    assert capture.observations.is_complete


def test_the_worker_pod_keeps_its_oom_state(capture: Capture) -> None:
    pod = capture.observations.pods[0]
    assert pod.last_terminated_reason == "OOMKilled"
    assert pod.restart_count == 4


def test_the_frontend_service_keeps_the_tier_selector(capture: Capture) -> None:
    service = capture.observations.services[0]
    assert service.name == "frontend"
    assert service.selector["tier"] == "web"


def test_the_endpoint_address_is_not_ready(capture: Capture) -> None:
    address = capture.observations.endpoints[0].addresses[0]
    assert address.address == "10.244.0.5"
    assert address.ready is False


def test_the_worker_and_loadgen_logs_are_captured(capture: Capture) -> None:
    assert WORKER_LOG in capture.observations.logs.worker
    assert LOADGEN_LOG in capture.observations.logs.loadgen


def test_every_collection_phase_is_timed(capture: Capture) -> None:
    timing = capture.observations.timing
    assert set(timing) == {"pods", "services", "endpoints", "http", "logs"}
    assert all(value >= 0 for value in timing.values())


def test_caller_phases_are_merged_with_the_measured_ones() -> None:
    observations = from_live(
        load_contract(),
        VARIANT,
        kubectl=FakeKubectl(),
        probe=FakeProbe(),
        sleep=FakeSleep(),
        captured_at=CAPTURED_AT,
        timing={"reset": 1.5, "pods": 99.0},
    )
    assert observations.timing["reset"] == 1.5
    assert observations.timing["pods"] != 99.0


def test_the_meta_marks_a_live_capture(capture: Capture) -> None:
    meta = capture.observations.meta
    assert meta.mode == MODE_LIVE
    assert meta.provenance == PROVENANCE_LIVE
    assert meta.variant == VARIANT
    assert meta.lab_commit == load_contract().source.commit
    assert meta.captured_at == CAPTURED_AT


def test_the_kubectl_calls_are_the_five_documented_ones(capture: Capture) -> None:
    assert len(capture.kubectl.get_calls) == 3
    for expected in (PODS_ARGS, SERVICE_ARGS, SLICE_ARGS):
        assert expected in capture.kubectl.get_calls
    assert len(capture.kubectl.text_calls) == 2
    for expected in (WORKER_LOG_ARGS, LOADGEN_LOG_ARGS):
        assert expected in capture.kubectl.text_calls


def test_three_probes_are_taken_with_the_route_and_the_timeout(capture: Capture) -> None:
    assert len(capture.observations.http) == 3
    assert [route for route, _ in capture.probe.calls] == [ROUTE, ROUTE, ROUTE]
    assert [timeout for _, timeout in capture.probe.calls] == [5.0, 5.0, 5.0]


def test_sleep_waits_between_the_probes(capture: Capture) -> None:
    assert capture.sleep.calls == [2.0, 2.0]


def test_a_kubectl_failure_raises_live_error() -> None:
    with pytest.raises(LiveError):
        from_live(
            load_contract(),
            VARIANT,
            kubectl=FailingKubectl(),
            probe=FakeProbe(),
            sleep=FakeSleep(),
            captured_at=CAPTURED_AT,
        )


def test_live_and_snapshot_documents_share_the_shape(capture: Capture) -> None:
    live = snapshot_document(capture.observations)
    restored = snapshot_document(from_snapshot(live))
    live["meta"]["mode"] = MODE_SNAPSHOT
    restored["meta"]["mode"] = MODE_SNAPSHOT
    assert live == restored


def field(item: Any, name: str) -> Any:
    return item[name] if isinstance(item, dict) else getattr(item, name)


def test_normalize_endpoints_pairs_each_address_with_its_own_readiness() -> None:
    slices = normalize_endpoints(copy.deepcopy(ENDPOINTS_PAYLOAD["items"]))
    pairs = [
        (field(address, "address"), field(address, "ready"))
        for item in slices
        for address in field(item, "addresses")
    ]
    assert pairs == [("10.244.0.5", False), ("10.244.0.6", True)]

"""Adversarial black-box tests for the observation layer, written from its guarantee."""

from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from verifier.contract import load_contract
from verifier.kubectl import LiveError
from verifier.live import from_live
from verifier.model import ObservationError
from verifier.observations import (
    from_snapshot,
    snapshot_document,
    snapshot_validator,
    to_snapshot,
)

LAB_COMMIT = "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0"
CAPTURED_AT = "2026-11-15T20:00:00Z"
ROUTE = "http://frontend.shop.svc.cluster.local/api/items"
MISSING, RAISES = object(), object()


def set_path(data, path, value):
    *steps, leaf = path.split(".")
    for step in steps:
        data = data[int(step)] if step.isdigit() else data[step]
    key = int(leaf) if isinstance(data, list) else leaf
    if value is MISSING:
        data.pop(key)
    else:
        data[key] = value


def document():
    pod = {
        "name": "worker-1",
        "ready": False,
        "restart_count": 4,
        "containers": [{"name": "worker", "ready": False, "restart_count": 4}],
    }
    return {
        "meta": {
            "lab_commit": LAB_COMMIT,
            "variant": "baseline",
            "captured_at": CAPTURED_AT,
            "provenance": "LIVE",
        },
        "pods": [pod],
        "services": [{"name": "frontend", "selector": {"app": "frontend"}, "ports": [80]}],
        "endpoints": [{"name": "s", "addresses": [{"address": "10.244.0.5", "ready": False}]}],
        "http": [{"route": ROUTE, "status": 503, "body": "", "error": None}],
        "worker": {"batch_size": 2500},
        "logs": {"worker": ["processed batch of 2500 items"]},
    }


CASES = [
    pytest.param("pods", {}, RAISES, id="pods-not-a-list"),
    pytest.param("services", "one", RAISES, id="services-not-a-list"),
    pytest.param("endpoints", {}, RAISES, id="endpoints-not-a-list"),
    pytest.param("http", 3, RAISES, id="http-not-a-list"),
    pytest.param("logs", [], RAISES, id="logs-not-an-object"),
    pytest.param("meta", [], RAISES, id="meta-not-an-object"),
    pytest.param("worker", [], RAISES, id="worker-not-an-object"),
    pytest.param("worker.batch_size", 0, RAISES, id="batch-size-zero"),
    pytest.param("worker.batch_size", "500", RAISES, id="batch-size-string"),
    pytest.param("pods.0.name", MISSING, RAISES, id="pod-name-missing"),
    pytest.param("pods.0.name", 7, RAISES, id="pod-name-not-a-string"),
    pytest.param("pods.0.containers.0.name", MISSING, RAISES, id="container-name-missing"),
    pytest.param("pods.0.restart_count", -1, RAISES, id="restart-count-negative"),
    pytest.param("pods.0.restart_count", True, RAISES, id="restart-count-boolean"),
    pytest.param("pods.0.restart_count", "4", RAISES, id="restart-count-string"),
    pytest.param("pods.0.restart_count", 4.0, RAISES, id="restart-count-float"),
    pytest.param("pods.0.ready", "true", RAISES, id="ready-string"),
    pytest.param("pods.0.ready", 1, RAISES, id="ready-int"),
    pytest.param("pods.0.ready", "yes", RAISES, id="ready-yes"),
    pytest.param("http.0.status", 600, RAISES, id="status-above-599"),
    pytest.param("http.0.status", "200", RAISES, id="status-string"),
    pytest.param("services.0.selector", ["app"], RAISES, id="selector-not-an-object"),
    pytest.param("services.0.ports.0", "80", RAISES, id="port-not-an-integer"),
    pytest.param("logs.worker", "processed batch", RAISES, id="worker-logs-not-a-list"),
    pytest.param(
        "meta.captured_at", "2026-11-15T20:00:00", ("captured_at",), id="captured-at-no-offset"
    ),
    pytest.param(
        "meta.captured_at", "yesterday", ("captured_at",), id="captured-at-not-a-timestamp"
    ),
    pytest.param("meta.captured_at", 20261115, RAISES, id="captured-at-not-a-string"),
    pytest.param("meta.lab_commit", 7, RAISES, id="lab-commit-not-a-string"),
    pytest.param("endpoints.0.addresses", "10.244.0.5", RAISES, id="addresses-not-a-list"),
    pytest.param("endpoints.0.addresses.0.address", MISSING, RAISES, id="address-without-address"),
    pytest.param("http.0.route", MISSING, RAISES, id="probe-without-route"),
    pytest.param("pods", MISSING, ("pods",), id="pods-missing"),
    pytest.param("services", MISSING, ("services",), id="services-missing"),
    pytest.param("endpoints", MISSING, ("endpoints",), id="endpoints-missing"),
    pytest.param("http", MISSING, ("http",), id="http-missing"),
    pytest.param("logs", MISSING, ("logs", "worker_logs"), id="logs-missing"),
    pytest.param("logs", {"loadgen": ["x"]}, ("worker_logs",), id="logs-without-worker"),
    pytest.param("meta.lab_commit", MISSING, ("lab_commit",), id="lab-commit-absent"),
    pytest.param("meta.lab_commit", "", ("lab_commit",), id="lab-commit-empty"),
    pytest.param("meta.variant", MISSING, ("variant",), id="variant-absent"),
    pytest.param("meta.variant", "", ("variant",), id="variant-empty"),
    pytest.param("meta.captured_at", MISSING, ("captured_at",), id="captured-at-absent"),
    pytest.param("meta.provenance", "live", ("provenance",), id="provenance-lowercase"),
    pytest.param("meta.captured_at", "2026-11-15T22:00:00+02:00", (), id="captured-at-offset"),
    pytest.param("pods", [], (), id="empty-pods"),
]


@pytest.mark.parametrize("path, value, expected", CASES)
def test_the_document_contract(path: str, value: Any, expected: Any) -> None:
    data = document()
    set_path(data, path, value)
    if expected is RAISES:
        with pytest.raises(ObservationError):
            from_snapshot(data)
    else:
        assert from_snapshot(data).missing_metadata == expected


class TestReading:
    def test_dedup_order_and_valid_variants(self) -> None:
        data = document()
        data["pods"].append(copy.deepcopy(data["pods"][0]))
        for pod in data["pods"]:
            del pod["restart_count"], pod["containers"][0]["restart_count"]
        assert from_snapshot(data).missing_metadata == ("restart_counts",)
        del data["http"], data["logs"]
        names = from_snapshot(data).missing_metadata
        assert names == ("restart_counts", "http", "logs", "worker_logs")
        data = document()
        data["extra"], data["pods"][0]["extra"] = {"note": "x"}, 1
        data["meta"]["captured_by"] = "operator"
        sidecar = dict(data["pods"][0]["containers"][0], name="sidecar", ready=True)
        data["pods"][0]["containers"].append(sidecar)
        data["http"] = [{"route": ROUTE, "status": None, "body": None, "error": "timeout"}]
        observations = from_snapshot(data)
        flags = {c.name: c.ready for c in observations.pods[0].containers}
        assert observations.is_complete and flags == {"worker": False, "sidecar": True}
        assert (observations.http[0].status, observations.http[0].error) == (None, "timeout")
        data = document()
        del data["meta"]
        names = from_snapshot(data).missing_metadata
        assert set(names) == {"lab_commit", "variant", "captured_at", "provenance"}


class TestFilesAndWriting:
    @pytest.mark.parametrize("kind", ["NaN", "Infinity", "-Infinity", "utf8", "dir"])
    def test_unreadable_documents_raise(self, tmp_path: Path, kind: str) -> None:
        path = tmp_path / "o.json"
        if kind == "dir":
            path = tmp_path
        elif kind == "utf8":
            path.write_bytes(b'{"meta": {"variant": "\xff\xfe"}}')
        else:
            data = document()
            data["http"][0]["duration_seconds"] = float(kind)
            path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ObservationError):
            from_snapshot(path)

    def test_writing_reloading_and_a_second_write_are_stable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = from_snapshot(document())
        path = tmp_path / "out" / "snapshots" / "observation.json"
        to_snapshot(original, path)
        path.write_text("not json at all", encoding="utf-8")
        to_snapshot(original, path)
        text = path.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
        assert "timing" not in text and from_snapshot(path).is_complete
        written = snapshot_document(original)
        assert set(written) == {
            "meta",
            "pods",
            "services",
            "endpoints",
            "http",
            "logs",
            "worker",
        }
        first, second = to_snapshot(original, tmp_path / "a"), to_snapshot(original, tmp_path / "b")
        assert first.read_bytes() == second.read_bytes()
        restored = from_snapshot(first)
        assert restored.meta.mode == "SNAPSHOT"
        assert replace(restored, timing={}) == replace(
            original, meta=replace(original.meta, mode="SNAPSHOT"), timing={}
        )

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("subprocess call")

        monkeypatch.setattr(subprocess, "run", refuse)
        assert from_snapshot(path).is_complete and from_snapshot(document()).is_complete


SERVICE = {"metadata": {"name": "frontend"}, "spec": {"selector": {"app": "frontend"}, "ports": []}}
OK_PROBE = {"status": 200, "body": "{}", "error": None}
DEPLOYMENT = json.loads(
    '{"spec": {"template": {"spec": {"containers": ['
    '{"name": "worker", "env": [{"name": "BATCH_SIZE", "value": "500"}]}]}}}}'
)


def kubectl_pods(*statuses):
    status = {"phase": "Running"} | ({"containerStatuses": list(statuses)} if statuses else {})
    return {"items": [{"metadata": {"name": "w", "uid": "abc"}, "status": status}]}


def kubectl_endpoints(*pairs):
    items = [{"addresses": [a], "conditions": {"ready": r}} for a, r in pairs]
    return {"items": [{"metadata": {"name": "s"}, "endpoints": items}]}


class FakeKubectl:
    """A kubectl with only `get(args)` and `text(args)`, recording both."""

    def __init__(self, pods, endpoints=None, loadgen_error=False):
        self.pods, self.endpoints, self.loadgen_error = pods, endpoints, loadgen_error
        self.text_calls = []

    def get(self, args):
        if "pods" in args:
            return copy.deepcopy(self.pods)
        if "service" in args:
            return copy.deepcopy(SERVICE)
        if "deployment" in args:
            return copy.deepcopy(DEPLOYMENT)
        return copy.deepcopy(self.endpoints or kubectl_endpoints(("10.244.0.5", False)))

    def text(self, args):
        self.text_calls.append(list(args))
        if any(str(argument).startswith("pod/") for argument in args):
            return "processed batch of 500 items\n"
        if self.loadgen_error:
            raise LiveError("loadgen log failed")
        return "GET /api/items -> 503\n"


def fake_probe(result):
    return lambda route, *a, **k: dict(result, route=route)


def capture(contract, kubectl, probe=None):
    extra = {"probe": fake_probe(probe or OK_PROBE), "sleep": lambda seconds: None}
    return from_live(contract, "baseline", kubectl=kubectl, captured_at=CAPTURED_AT, **extra)


class TestLive:
    def test_live_capture(self) -> None:
        contract = load_contract()
        pods = kubectl_pods(
            {"name": "worker", "ready": False, "restartCount": 3},
            {"name": "sidecar", "ready": True, "restartCount": 1},
        )
        pod = capture(contract, FakeKubectl(pods)).pods[0]
        assert (pod.ready, pod.restart_count) == (False, 4)
        assert [(c.ready, c.restart_count) for c in pod.containers] == [(False, 3), (True, 1)]
        bare = capture(contract, FakeKubectl(kubectl_pods())).pods[0]
        assert (bare.ready, bare.restart_count, bare.containers) == (False, 0, ())
        stateless = kubectl_pods({"name": "worker", "ready": True, "restartCount": 0})
        status = capture(contract, FakeKubectl(stateless)).pods[0].containers[0]
        assert (
            status.waiting_reason is status.last_terminated_reason is status.last_exit_code is None
        )
        slices = kubectl_endpoints(("10.244.0.5", True), ("10.244.0.6", False))
        addresses = capture(contract, FakeKubectl(kubectl_pods(), slices)).endpoints[0].addresses
        pairs = [(a.address, a.ready) for a in addresses]
        assert pairs == [("10.244.0.5", True), ("10.244.0.6", False)]
        kubectl = FakeKubectl(kubectl_pods(), loadgen_error=True)
        observations = capture(contract, kubectl, {"error": "timeout"})
        assert observations.logs.loadgen == ()
        assert "processed batch of 500 items" in observations.logs.worker
        assert [p.status for p in observations.http] == [None, None, None]
        assert [p.error for p in observations.http] == ["timeout"] * 3
        assert observations.http[0].route == ROUTE
        assert (observations.meta.mode, observations.meta.provenance) == ("LIVE", "LIVE")
        assert observations.meta.lab_commit == contract.source.commit
        assert [call[-1] for call in kubectl.text_calls] == ["--since=60s", "--since=60s"]


class TestSnapshotSchema:
    def test_the_schema_accepts_the_reference_document(self) -> None:
        assert snapshot_validator().errors(document()) == []

    def test_the_schema_accepts_a_document_missing_whole_sections(self) -> None:
        assert snapshot_validator().errors({"meta": document()["meta"]}) == []

    @pytest.mark.parametrize(
        "path, value",
        [
            pytest.param("pods", {}, id="pods-not-a-list"),
            pytest.param("logs", [], id="logs-not-an-object"),
            pytest.param("meta", [], id="meta-not-an-object"),
            pytest.param("pods.0.ready", "true", id="ready-not-a-boolean"),
            pytest.param("pods.0.restart_count", -1, id="restart-count-negative"),
            pytest.param("http.0.status", "200", id="status-not-an-integer"),
        ],
    )
    def test_the_schema_rejects_a_wrong_type(self, path: str, value: Any) -> None:
        data = document()
        set_path(data, path, value)
        assert snapshot_validator().errors(data) != []

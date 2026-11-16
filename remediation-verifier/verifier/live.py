"""Collect a LIVE observation through kubectl and HTTP.

This is the only module that assembles a live capture, and nothing on the
snapshot path imports it. That keeps the promise that SNAPSHOT mode cannot call
Kubernetes: the dependency is absent, not disabled by a flag.

The collector converts kubectl output into the snapshot shape and then parses
it with `observations.from_snapshot`, so LIVE and SNAPSHOT share one parser and
one set of dataclasses.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from verifier.kubectl import (
    DEFAULT_HTTP_TIMEOUT_S,
    Kubectl,
    LiveError,
    ProbeFn,
    SleepFn,
    probe_http,
)
from verifier.model import Contract, Fault, Observations
from verifier.observations import MODE_LIVE, PROVENANCE_LIVE, from_snapshot

LOGGER = logging.getLogger(__name__)

# The load generator is declared in the pinned lab app manifest.
# Source: app/shop.yaml#L236-L243 at the contract commit.
LOADGEN_DEPLOYMENT = "loadgen"


def from_live(
    contract: Contract,
    variant: str,
    *,
    kubectl: Kubectl | None = None,
    probe: ProbeFn = probe_http,
    sleep: SleepFn = time.sleep,
    captured_at: str | None = None,
    timing: Mapping[str, float] | None = None,
) -> Observations:
    """Collect the observation a variant produces, for a LIVE run.

    Each collection phase is timed and returned in `timing`. A caller may pass
    its own phases, such as the runner's reset and fix steps; the measured
    phases overwrite a name they share, because the measurement is the truth.
    """
    runner = kubectl if kubectl is not None else Kubectl()
    worker = fault_with(contract, "deployment")
    frontend = fault_with(contract, "service")
    window = contract.oracle.observation.window_seconds
    phases: dict[str, float] = dict(timing or {})

    started = time.monotonic()
    batch_size = _batch_size_of(runner, worker)
    phases["deployment"] = _elapsed(started)

    started = time.monotonic()
    pods = _pods_of(runner, worker)
    phases["pods"] = _elapsed(started)

    started = time.monotonic()
    services = _services_of(runner, frontend)
    phases["services"] = _elapsed(started)

    started = time.monotonic()
    endpoints = _endpoints_of(runner, frontend)
    phases["endpoints"] = _elapsed(started)

    started = time.monotonic()
    http = _probe_route(frontend, probe, sleep)
    phases["http"] = _elapsed(started)

    started = time.monotonic()
    logs = {
        "worker": _log_lines(runner, worker, window, pods),
        "loadgen": _loadgen_lines(runner, worker, window),
    }
    phases["logs"] = _elapsed(started)

    document: dict[str, Any] = {
        "meta": {
            "lab_commit": contract.source.commit,
            "variant": variant,
            "mode": MODE_LIVE,
            "captured_at": captured_at or _utc_now(),
            "provenance": PROVENANCE_LIVE,
        },
        "pods": pods,
        "services": services,
        "endpoints": endpoints,
        "http": http,
        "logs": logs,
        "worker": {"batch_size": batch_size},
    }
    parsed = from_snapshot(document)
    LOGGER.debug("captured live observation for %s with %d pods", variant, len(parsed.pods))
    return replace(parsed, meta=replace(parsed.meta, mode=MODE_LIVE), timing=phases)


def normalize_pods(items: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert `kubectl get pods -o json` items into the snapshot shape."""
    return [_pod(item) for item in items]


def normalize_services(items: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert `kubectl get services -o json` items into the snapshot shape."""
    services = []
    for item in items:
        node = _mapping(item)
        spec = _mapping(node.get("spec"))
        services.append(
            {
                "name": _text(_mapping(node.get("metadata")).get("name")),
                "selector": {
                    str(key): _text(value) for key, value in _mapping(spec.get("selector")).items()
                },
                "ports": [
                    _number(_mapping(port).get("port")) for port in _sequence(spec.get("ports"))
                ],
                "cluster_ip": _text(spec.get("clusterIP")),
            }
        )
    return services


def normalize_endpoints(items: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert EndpointSlice items into the snapshot shape, one address each."""
    slices = []
    for item in items:
        node = _mapping(item)
        addresses: list[dict[str, Any]] = []
        for endpoint in _sequence(node.get("endpoints")):
            entry = _mapping(endpoint)
            ready = _mapping(entry.get("conditions")).get("ready") is True
            addresses.extend(
                {"address": _text(address), "ready": ready}
                for address in _sequence(entry.get("addresses"))
            )
        slices.append(
            {"name": _text(_mapping(node.get("metadata")).get("name")), "addresses": addresses}
        )
    return slices


def normalize_logs(text: str) -> list[str]:
    """Split kubectl log output into lines, keeping empty output empty."""
    return text.splitlines()


def _batch_size_of(runner: Kubectl, worker: Fault) -> int | None:
    """Read BATCH_SIZE from the worker Deployment, the source the contract names."""
    if not worker.deployment:
        raise LiveError("the worker fault declares no deployment")
    document = runner.get(["-n", worker.namespace, "get", "deployment", worker.deployment])
    template = _mapping(_mapping(document.get("spec")).get("template"))
    pod_spec = _mapping(template.get("spec"))
    for container in _sequence(pod_spec.get("containers")):
        node = _mapping(container)
        if worker.container and node.get("name") != worker.container:
            continue
        for variable in _sequence(node.get("env")):
            entry = _mapping(variable)
            if entry.get("name") == "BATCH_SIZE":
                return _positive_int(entry.get("value"))
    return None


def _pods_of(runner: Kubectl, worker: Fault) -> list[dict[str, Any]]:
    selector = worker.selector or ""
    if not selector:
        raise LiveError("the worker fault declares no selector to list pods")
    document = runner.get(["-n", worker.namespace, "get", "pods", "-l", selector])
    return normalize_pods(_sequence(document.get("items")))


def _services_of(runner: Kubectl, frontend: Fault) -> list[dict[str, Any]]:
    if not frontend.service:
        raise LiveError("the frontend fault declares no service")
    document = runner.get(["-n", frontend.namespace, "get", "service", frontend.service])
    return normalize_services([document])


def _endpoints_of(runner: Kubectl, frontend: Fault) -> list[dict[str, Any]]:
    selector = frontend.endpoint_slice_selector or ""
    if not selector:
        raise LiveError("the frontend fault declares no endpoint slice selector")
    document = runner.get(["-n", frontend.namespace, "get", "endpointslices", "-l", selector])
    return normalize_endpoints(_sequence(document.get("items")))


def _probe_route(frontend: Fault, probe: ProbeFn, sleep: SleepFn) -> list[dict[str, Any]]:
    if not frontend.http_route:
        raise LiveError("the frontend fault declares no http route")
    interval = frontend.request_interval_seconds or 0.0
    timeout_s = frontend.request_timeout_seconds or DEFAULT_HTTP_TIMEOUT_S
    probes = []
    for index in range(frontend.requests or 1):
        if index:
            sleep(interval)
        probes.append(dict(probe(frontend.http_route, timeout_s)))
    return probes


def _log_lines(
    runner: Kubectl, worker: Fault, window: int, pods: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Read the log of the pod the capture counted, never of a pod being replaced.

    `kubectl logs deployment/x` picks one pod and says which, and during a
    rollout that can be the pod that is terminating. The log has to come from
    the pod whose batch size the capture read, or a line from an earlier
    container instance could satisfy the check.
    """
    name = _current_pod_name(pods)
    if name is None:
        LOGGER.warning("no current worker pod, so no worker log was captured")
        return []
    args = ["-n", worker.namespace, "logs", f"pod/{name}", f"--since={window}s"]
    return normalize_logs(runner.text(args))


def _current_pod_name(pods: Sequence[Mapping[str, Any]]) -> str | None:
    for pod in pods:
        name = pod.get("name")
        if isinstance(name, str) and name and not pod.get("terminating"):
            return name
    return None


def _loadgen_lines(runner: Kubectl, worker: Fault, window: int) -> list[str]:
    args = [
        "-n",
        worker.namespace,
        "logs",
        f"deployment/{LOADGEN_DEPLOYMENT}",
        f"--since={window}s",
    ]
    try:
        return normalize_logs(runner.text(args))
    except LiveError as error:
        # The tail supports the report, so its absence does not abort a capture.
        LOGGER.warning("no loadgen logs captured: %s", error)
        return []


def fault_with(contract: Contract, attribute: str) -> Fault:
    for fault in contract.oracle.faults.values():
        if getattr(fault, attribute):
            return fault
    raise LiveError(f"the contract declares no fault with a {attribute}")


def _pod(item: Any) -> dict[str, Any]:
    node = _mapping(item)
    metadata = _mapping(node.get("metadata"))
    status = _mapping(node.get("status"))
    containers = [_mapping(entry) for entry in _sequence(status.get("containerStatuses"))]
    return {
        "name": _text(metadata.get("name")),
        "uid": _text(metadata.get("uid")),
        "phase": _text(status.get("phase")) or "Unknown",
        "ready": bool(containers)
        and all(container.get("ready") is True for container in containers),
        "restart_count": sum(_number(container.get("restartCount")) for container in containers),
        "waiting_reason": _first_reason(containers, "state", "waiting"),
        "last_terminated_reason": _first_reason(containers, "lastState", "terminated"),
        "last_exit_code": _first_exit_code(containers),
        "terminating": bool(metadata.get("deletionTimestamp")),
        "containers": [_container(container) for container in containers],
    }


def _container(container: Mapping[str, Any]) -> dict[str, Any]:
    state = _mapping(container.get("state"))
    last_state = _mapping(container.get("lastState"))
    return {
        "name": _text(container.get("name")),
        "ready": container.get("ready") is True,
        "restart_count": _number(container.get("restartCount")),
        "waiting_reason": _reason(state, "waiting"),
        "last_terminated_reason": _reason(last_state, "terminated"),
        "last_exit_code": _exit_code(last_state),
    }


def _first_reason(containers: Sequence[Mapping[str, Any]], key: str, phase: str) -> str | None:
    for container in containers:
        reason = _reason(_mapping(container.get(key)), phase)
        if reason is not None:
            return reason
    return None


def _first_exit_code(containers: Sequence[Mapping[str, Any]]) -> int | None:
    for container in containers:
        code = _exit_code(_mapping(container.get("lastState")))
        if code is not None:
            return code
    return None


def _reason(state: Mapping[str, Any], phase: str) -> str | None:
    reason = _mapping(state.get(phase)).get("reason")
    return reason if isinstance(reason, str) else None


def _exit_code(state: Mapping[str, Any]) -> int | None:
    code = _mapping(state.get("terminated")).get("exitCode")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None

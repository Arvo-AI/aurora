"""A simulated shop cluster for the runner tests.

The simulator answers the same kubectl calls `verifier.live` makes and changes
its state the way the real objects do, so a whole variant can run without
Docker. It is a state machine, not a recording: a fix has to change the state,
or a test would pass with a runner that never ran the fix.

The scenarios name the failure modes worth pinning:

| Scenario | What the fix leaves behind |
|---|---|
| `full` | Both faults gone, one clean worker pod |
| `partial_worker_only` | Worker fixed, selector still matches nothing |
| `partial_frontend_only` | Selector fixed, worker still OOMKilled |
| `noop` | Nothing changed |
| `no_fault` | The break did not take, so the baseline cannot confirm |
| `stale_pod` | Fix worked, the OOMKilled pod is still terminating |
| `flapping` | Fix applied, the worker restarts during the window |
| `unready_endpoints` | Address appears but never reports ready |
| `rollout_timeout` | `kubectl rollout status` runs out of budget; the checks decide |
| `rollout_failure` | `kubectl rollout status` is refused, which is an execution error |
| `no_endpoints` | The slice stays empty until the wait gives up |
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from verifier.kubectl import LiveError, RolloutTimeout

HEALTHY_ADDRESS = ("10.244.0.11", True)
MEMORY_LIMIT = re.compile(r"--limits=memory=(\d+)Mi")

FULL_STEPS = (
    "kubectl -n shop patch svc frontend --type=json "
    '-p=\'[{"op":"remove","path":"/spec/selector/tier"}]\'',
    "kubectl -n shop set env deploy/worker BATCH_SIZE=500",
)


class SimulatedCluster:
    """A kubectl-shaped cluster whose state the commands actually change."""

    def __init__(self, scenario: str = "full") -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, Any]] = []
        self.captures_since_fix = 0
        self.fixed = False
        self.reset_state()

    def reset_state(self) -> None:
        self.batch_size = 500
        self.memory_limit_mib = 192
        self.selector_tier = False
        self.worker_uid = "worker-w1"
        self.worker_restarts = 0
        self.worker_reason: str | None = None
        self.worker_waiting: str | None = None
        self.worker_ready = True
        self.worker_terminating = False
        self.stale_uid: str | None = None
        self.endpoints: list[tuple[str, bool]] = [HEALTHY_ADDRESS]
        self.http_status: int | None = 200

    def break_scenario(self) -> None:
        """Inject both faults, unless the scenario is the one where nothing breaks."""
        self.reset_state()
        if self.scenario == "no_fault":
            return
        self.batch_size = 2500
        self.selector_tier = True
        self.worker_uid = "worker-w2"
        self.worker_restarts = 7
        self.worker_reason = "OOMKilled"
        self.worker_waiting = "CrashLoopBackOff"
        self.worker_ready = False
        self.endpoints = []
        self.http_status = None

    def apply_fix(self, steps: Sequence[str]) -> None:
        self.fixed = True
        self.captures_since_fix = 0
        if any("BATCH_SIZE=500" in step for step in steps):
            self.batch_size = 500
        if any("remove" in step and "tier" in step for step in steps):
            self.selector_tier = False
        for step in steps:
            match = MEMORY_LIMIT.search(step)
            if match:
                self.memory_limit_mib = int(match.group(1))
        self._settle()

    def wait_rollout(self, timeout_s: float) -> None:
        if self.scenario == "rollout_timeout":
            raise RolloutTimeout(f"the worker rollout did not finish in {timeout_s:g}s")
        if self.scenario == "rollout_failure":
            raise LiveError("kubectl rollout status exited 1: Forbidden")

    def wait_endpoints(self, timeout_s: float, poll_s: float) -> bool:
        return bool(self.endpoints)

    def capture(self, variant: str) -> None:
        """Let time pass between the two captures of a window."""
        if not self.fixed:
            return
        self.captures_since_fix += 1
        if self.scenario == "flapping" and self.captures_since_fix >= 2:
            self.worker_restarts = 2
            self.worker_reason = "OOMKilled"
            self.worker_waiting = "CrashLoopBackOff"
            self.worker_ready = False

    def drop_terminating(self) -> None:
        """Finish the termination a rollout left behind."""
        self.stale_uid = None

    def get(self, args: Sequence[str]) -> Mapping[str, Any]:
        self.calls.append(("get", tuple(args)))
        joined = " ".join(args)
        if "get deployment" in joined:
            return self.deployment()
        if "get pods" in joined:
            return self.pods()
        if "get service" in joined:
            return self.service()
        if "get endpointslices" in joined:
            return self.endpoint_slices()
        raise LiveError(f"unexpected kubectl get: {joined}")

    def text(self, args: Sequence[str]) -> str:
        self.calls.append(("text", tuple(args)))
        joined = " ".join(args)
        if "logs pod/" in joined:
            if self.worker_ready and not self.worker_waiting:
                line = f"processed batch of {self.batch_size} items"
                return f"{line}\n{line}\n"
            return f"worker started, batch size {self.batch_size}\n"
        if "logs deployment/loadgen" in joined:
            code = self.http_status if self.http_status is not None else "timeout"
            return f"GET /api/items -> {code}\n"
        raise LiveError(f"unexpected kubectl text: {joined}")

    def probe(self, route: str, timeout_s: float) -> Mapping[str, Any]:
        self.calls.append(("probe", route))
        if self.http_status is None:
            return {"route": route, "error": "connection refused", "status": None}
        body = json.dumps({"items": ["kettle", "mug", "teapot"]})
        return {"route": route, "status": self.http_status, "body": body, "error": None}

    def deployment(self) -> Mapping[str, Any]:
        return {
            "metadata": {"name": "worker"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "worker",
                                "env": [{"name": "BATCH_SIZE", "value": str(self.batch_size)}],
                            }
                        ]
                    }
                }
            },
        }

    def pods(self) -> Mapping[str, Any]:
        items = [
            self.pod(
                self.worker_uid,
                restarts=self.worker_restarts,
                reason=self.worker_reason,
                waiting=self.worker_waiting,
                ready=self.worker_ready,
                terminating=self.worker_terminating,
            )
        ]
        if self.stale_uid:
            items.append(
                self.pod(
                    self.stale_uid,
                    restarts=7,
                    reason="OOMKilled",
                    waiting="CrashLoopBackOff",
                    ready=False,
                    terminating=True,
                )
            )
        return {"items": items}

    def service(self) -> Mapping[str, Any]:
        selector = {"app": "frontend"}
        if self.selector_tier:
            selector["tier"] = "web"
        return {
            "metadata": {"name": "frontend"},
            "spec": {"selector": selector, "ports": [{"port": 80}], "clusterIP": "10.96.0.10"},
        }

    def endpoint_slices(self) -> Mapping[str, Any]:
        endpoints = [
            {"addresses": [address], "conditions": {"ready": ready}}
            for address, ready in self.endpoints
        ]
        return {"items": [{"metadata": {"name": "frontend-abc"}, "endpoints": endpoints}]}

    def pod(
        self,
        name: str,
        *,
        restarts: int,
        reason: str | None,
        waiting: str | None,
        ready: bool,
        terminating: bool,
    ) -> Mapping[str, Any]:
        metadata: dict[str, Any] = {"name": name, "uid": name}
        if terminating:
            metadata["deletionTimestamp"] = "2026-11-15T20:00:30Z"
        state: Mapping[str, Any] = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
        last: Mapping[str, Any] = (
            {"terminated": {"reason": reason, "exitCode": 137}} if reason else {}
        )
        return {
            "metadata": metadata,
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "worker",
                        "ready": ready,
                        "restartCount": restarts,
                        "state": state,
                        "lastState": last,
                    }
                ],
            },
        }

    def _settle(self) -> None:
        """Recompute the state the applied commands leave behind."""
        if self._worker_oom():
            self.worker_reason = "OOMKilled"
            self.worker_waiting = "CrashLoopBackOff"
            self.worker_restarts = 7
            self.worker_ready = False
        else:
            self.worker_reason = None
            self.worker_waiting = None
            self.worker_restarts = 0
            self.worker_ready = True
            if self.worker_uid == "worker-w2":
                self.worker_uid = "worker-w3"
        if self.selector_tier:
            self.endpoints = []
            self.http_status = None
        else:
            self.endpoints = [HEALTHY_ADDRESS]
            self.http_status = 200
        self._apply_knob()

    def _worker_oom(self) -> bool:
        """A batch of 100KiB items against the current limit.

        500 items is about 49MiB and 2500 is about 244MiB, the figure the
        runbook measures (`scenarios/12-two-faults-oom-and-selector/README.md#L160`).
        """
        return self.batch_size * 100 / 1024 > self.memory_limit_mib

    def _apply_knob(self) -> None:
        healthy = not self._worker_oom() and not self.selector_tier
        if not healthy:
            return
        if self.scenario == "stale_pod":
            self.stale_uid = "worker-w2"
        if self.scenario == "unready_endpoints":
            self.endpoints = [(HEALTHY_ADDRESS[0], False)]
            self.http_status = None
        if self.scenario == "no_endpoints":
            self.endpoints = []
            self.http_status = None

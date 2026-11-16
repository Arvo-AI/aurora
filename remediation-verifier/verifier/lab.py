"""The lab checkout: where the cluster is, and how a command reaches it.

Every command a LIVE run sends to the cluster passes through this module, so
the kubeconfig is decided in one place. `lab.sh` uses `<lab>/.kubeconfig` and
the `kind-<cluster>` context; the verifier passes both explicitly and hands the
child a minimal environment, so a `KUBECONFIG` exported in the shell cannot
point a run at another cluster.

`LiveSession` is the LIVE half of the runner's `Session` protocol: reset,
break, fix, wait and capture. The runner never learns how a cluster is driven,
which is what lets the whole sequence be tested against a simulated one.
"""

from __future__ import annotations

import logging
import math
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verifier.kubectl import (
    DEFAULT_COMMAND_TIMEOUT_S,
    Kubectl,
    LiveError,
    ProbeFn,
    probe_http,
)
from verifier.live import fault_with, from_live
from verifier.model import Contract, Observations
from verifier.observations import MODE_LIVE

LOGGER = logging.getLogger(__name__)

DEFAULT_CLUSTER = "aurora-lab"
KUBECONFIG_NAME = ".kubeconfig"
CONTEXT_PREFIX = "kind-"

# Only what a client needs to run. `KUBECONFIG` is replaced, never inherited.
PASSED_ENV_KEYS = ("PATH", "HOME", "LANG", "SYSTEMROOT", "TMPDIR")


@dataclass(frozen=True)
class Lab:
    """A checkout of aurora-lab at the pinned commit."""

    directory: Path
    cluster: str = DEFAULT_CLUSTER

    @property
    def kubeconfig(self) -> Path:
        """The file lab.sh exports for this cluster (lab.sh#L14-L18)."""
        if self.cluster == DEFAULT_CLUSTER:
            return self.directory / KUBECONFIG_NAME
        return self.directory / f"{KUBECONFIG_NAME}-{self.cluster}"

    @property
    def context(self) -> str:
        return f"{CONTEXT_PREFIX}{self.cluster}"

    @property
    def script(self) -> Path:
        return self.directory / "lab.sh"

    def kubectl(self, *, timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S) -> Kubectl:
        """Return a kubectl bound to this lab, for every call the session makes."""
        return Kubectl(
            timeout_s=timeout_s,
            global_args=("--kubeconfig", str(self.kubeconfig), "--context", self.context),
        )

    def environment(self) -> dict[str, str]:
        """Return the child environment: this lab's kubeconfig, nothing personal."""
        environment = {key: os.environ[key] for key in PASSED_ENV_KEYS if key in os.environ}
        environment["KUBECONFIG"] = str(self.kubeconfig)
        return environment

    def run(self, argv: Sequence[str], *, timeout_s: float) -> str:
        """Run one lab command and return stdout, raising LiveError on failure."""
        command = list(argv)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=self.environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LiveError(f"cannot run {command[0]}: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise LiveError(f"{' '.join(command)} exited {completed.returncode}: {detail}")
        return completed.stdout


class LiveSession:
    """Drive the pinned lab: reset, break, fix, wait and capture."""

    mode = MODE_LIVE

    def __init__(
        self,
        contract: Contract,
        lab: Lab,
        *,
        probe: ProbeFn = probe_http,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], str] | None = None,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        self._contract = contract
        self._lab = lab
        self._probe = probe
        self._sleep = sleep
        self._clock = clock or utc_now
        self._kubectl = lab.kubectl(timeout_s=command_timeout_s)
        self._worker = fault_with(contract, "deployment")
        self._frontend = fault_with(contract, "service")

    def break_scenario(self, timeout_s: float) -> None:
        """Break the scenario. `lab.sh break` resets first, so nothing leaks in."""
        self._lab.run(
            ["bash", str(self._lab.script), "break", self._contract.scenario],
            timeout_s=timeout_s,
        )

    def apply_fix(self, steps: Sequence[str], timeout_s: float) -> None:
        for step in steps:
            self._kubectl.text(fix_step_args(step))

    def wait_rollout(self, timeout_s: float) -> None:
        deployment = self._worker.deployment
        if not deployment:
            raise LiveError("the worker fault declares no deployment to roll out")
        self._kubectl.text(
            [
                "-n",
                self._worker.namespace,
                "rollout",
                "status",
                f"deployment/{deployment}",
                f"--timeout={math.ceil(timeout_s)}s",
            ]
        )

    def wait_endpoints(self, timeout_s: float, poll_s: float) -> bool:
        """Wait for a non-empty slice. Readiness is a check, not a precondition.

        An empty slice after the budget is not an execution error: it is what a
        fix that left the selector broken looks like, and the two endpoint
        checks turn it into a REJECTED verdict with evidence. Only a kubectl
        failure raises, because only then did the verifier fail to look.
        """
        selector = self._frontend.endpoint_slice_selector
        if not selector:
            raise LiveError("the frontend fault declares no endpoint slice selector")
        polls = _poll_count(timeout_s, poll_s)
        for attempt in range(polls):
            document = self._kubectl.get(
                ["-n", self._frontend.namespace, "get", "endpointslices", "-l", selector]
            )
            if endpoint_addresses(document):
                return True
            if attempt + 1 < polls:
                self._sleep(poll_s)
        LOGGER.info("the frontend slice stayed empty for %gs", timeout_s)
        return False

    def capture(self, variant: str) -> Observations:
        return from_live(
            self._contract,
            variant,
            kubectl=self._kubectl,
            probe=self._probe,
            sleep=self._sleep,
            captured_at=self._clock(),
        )


def fix_step_args(step: str) -> list[str]:
    """Split a contract fix step into kubectl arguments, without a shell.

    The steps are literals from the frozen contract. They are still parsed
    without a shell, so a step can only run kubectl with the arguments the
    contract names, and never an arbitrary command.
    """
    parts = shlex.split(step)
    if not parts or Path(parts[0]).name != "kubectl":
        raise LiveError(f"a fix step has to start with kubectl: {step!r}")
    return parts[1:]


def endpoint_addresses(document: Mapping[str, Any]) -> int:
    """Count the addresses in an EndpointSliceList, tolerating another shape."""
    items = document.get("items")
    if not isinstance(items, list):
        return 0
    total = 0
    for item in items:
        endpoints = item.get("endpoints") if isinstance(item, Mapping) else None
        if not isinstance(endpoints, list):
            continue
        for endpoint in endpoints:
            addresses = endpoint.get("addresses") if isinstance(endpoint, Mapping) else None
            if isinstance(addresses, list):
                total += len(addresses)
    return total


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _poll_count(timeout_s: float, poll_s: float) -> int:
    if poll_s <= 0:
        return 1
    return max(1, math.ceil(timeout_s / poll_s))

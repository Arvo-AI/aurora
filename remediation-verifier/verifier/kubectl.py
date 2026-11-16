"""The two things a LIVE run needs: kubectl, and a request through the Service.

No Kubernetes client library: the verifier shells out to `kubectl` with an
explicit timeout. The HTTP check runs through `kubectl exec` in the load
generator, because the host cannot resolve a cluster-internal Service name, and
the load generator is the client the runbook reads.

Only `verifier.live` and `verifier.lab` import this module.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_COMMAND_TIMEOUT_S = 10.0
DEFAULT_HTTP_TIMEOUT_S = 5.0

STATUS_PATTERN = re.compile(r"HTTP/\d(?:\.\d)?\s+(\d{3})")
EXIT_MARKER = "RV_EXIT:"

ProbeFn = Callable[[str, float], Mapping[str, Any]]
SleepFn = Callable[[float], None]


class LiveError(RuntimeError):
    """A kubectl command or an HTTP probe could not be executed."""


class RolloutTimeout(LiveError):
    """The rollout did not finish before its budget.

    A fix that never touches the worker leaves it crash-looping, so the
    rollout never finishes. That is a state for the checks to read, not a
    failure to look, and it is why this type exists apart from `LiveError`.
    """


@dataclass(frozen=True)
class Kubectl:
    """Small subprocess wrapper. No Kubernetes client, only the binary.

    `global_args` are inserted between the binary and the subcommand, which is
    where `--kubeconfig` and `--context` have to be for every call to resolve
    against the lab cluster and nowhere else.
    """

    binary: str = "kubectl"
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S
    global_args: tuple[str, ...] = ()

    def get(self, args: Sequence[str]) -> Mapping[str, Any]:
        """Run kubectl with `-o json` and return the parsed object."""
        output = self._run([*args, "-o", "json"])
        try:
            document = json.loads(output)
        except json.JSONDecodeError as error:
            raise LiveError(f"kubectl did not return JSON: {error}") from error
        if not isinstance(document, Mapping):
            raise LiveError("kubectl returned a JSON document that is not an object")
        return document

    def text(self, args: Sequence[str]) -> str:
        """Run kubectl and return its standard output."""
        return self._run(args)

    def _run(self, args: Sequence[str]) -> str:
        command = [self.binary, *self.global_args, *args]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LiveError(f"kubectl failed to run: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.strip()
            raise LiveError(f"kubectl {' '.join(args)} exited {result.returncode}: {detail}")
        return result.stdout


def probe_in_cluster(kubectl: Kubectl, namespace: str, deployment: str) -> ProbeFn:
    """Return a probe that asks the client inside the cluster, not the host.

    The host cannot resolve `frontend.shop.svc.cluster.local`, so the request
    runs in the load generator, the same client the runbook reads. The shell
    always exits 0 and reports wget's own status on the last line, so a 503 is
    evidence about the Service and not a command failure.
    """

    def probe(route: str, timeout_s: float) -> Mapping[str, Any]:
        started = time.monotonic()
        command = (
            f"wget -q -S -O - -T {math.ceil(timeout_s)} {route} 2>&1; "
            'code=$?; echo; echo "RV_EXIT:$code"'
        )
        try:
            output = kubectl.text(
                ["-n", namespace, "exec", f"deployment/{deployment}", "--", "sh", "-c", command]
            )
        except LiveError as error:
            return {
                "route": route,
                "status": None,
                "body": None,
                "duration_seconds": _elapsed(started),
                "error": str(error),
            }
        return parse_wget(route, output, started)

    return probe


def parse_wget(route: str, output: str, started: float) -> dict[str, Any]:
    """Read the last status line and the JSON body out of wget's output.

    wget prints the response headers with `-S` and the body with `-O -`, both
    merged into one stream. The app answers with a one-line JSON object, so the
    body is the line that starts with `{`; anything else is a header or an
    error message.
    """
    status, body, errors = _read_wget(output)
    return {
        "route": route,
        "status": status,
        "body": body,
        "duration_seconds": _elapsed(started),
        "error": None if status is not None else (" ".join(errors) or "no HTTP status"),
    }


def _read_wget(output: str) -> tuple[int | None, str | None, list[str]]:
    """Split wget's merged stream into a status, a body and error lines."""
    status = None
    body = None
    errors: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith(EXIT_MARKER):
            continue
        match = STATUS_PATTERN.search(line)
        if match:
            status = int(match.group(1))
        elif line.startswith(("{", "[")):
            body = line if body is None else f"{body}\n{line}"
        elif status is None and line:
            errors.append(line)
    return status, body, errors


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)

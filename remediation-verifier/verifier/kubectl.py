"""Thin wrappers around the two things a LIVE run needs: kubectl and HTTP.

No Kubernetes client library: the verifier shells out to `kubectl` with an
explicit timeout, and reads the service through `urllib` so the request path is
the one a client would take. Only `verifier.live` imports this module.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from verifier.config import ENCODING

DEFAULT_COMMAND_TIMEOUT_S = 10.0
DEFAULT_HTTP_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 4096

ProbeFn = Callable[[str, float], Mapping[str, Any]]
SleepFn = Callable[[float], None]


class LiveError(RuntimeError):
    """A kubectl command or an HTTP probe could not be executed."""


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


def probe_http(route: str, timeout_s: float) -> Mapping[str, Any]:
    """Send one GET and return the evidence, including failures."""
    started = time.monotonic()
    request = urllib.request.Request(route, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read(MAX_BODY_BYTES).decode(ENCODING, errors="replace")
            return _probe(route, response.status, body, started)
    except urllib.error.HTTPError as error:
        body = error.read(MAX_BODY_BYTES).decode(ENCODING, errors="replace")
        return _probe(route, error.code, body, started)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"route": route, "error": str(error), "duration_seconds": _elapsed(started)}


def _probe(route: str, status: int | None, body: str, started: float) -> dict[str, Any]:
    return {
        "route": route,
        "status": status,
        "body": body,
        "duration_seconds": _elapsed(started),
    }


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)

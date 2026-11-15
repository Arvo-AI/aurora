"""Report whether this machine can run the remediation verifier.

The verifier has two modes. SNAPSHOT needs only Python and PyYAML; LIVE also
needs a container runtime and a Kubernetes cluster. This module answers one
question for the reader: what does this machine support, and what is missing.
It never fails because of an optional tool: only a broken runtime is a FAIL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from importlib import metadata
from pathlib import Path

LOGGER = logging.getLogger(__name__)

MINIMUM_PYTHON = (3, 11)
MINIMUM_PYYAML = (6, 0)
COMMAND_TIMEOUT_S = 10

# The lab commit every observation in this project is pinned to.
LAB_COMMIT = "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0"

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LAB_DIR = ROOT.parent.parent / "aurora-lab"

DEV_TOOLS = ("mypy", "pytest", "ruff")


class Status(StrEnum):
    """Outcome of one environment check."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    """One environment fact, with the detail a reader needs to act on it."""

    name: str
    status: Status
    detail: str


def _parse_version(text: str) -> tuple[int, ...]:
    """Turn "0.33.0" into (0, 33, 0) without third-party parsing."""
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _at_least(version: str, minimum: tuple[int, ...]) -> bool:
    parsed = _parse_version(version)
    padded = parsed + (0,) * max(0, len(minimum) - len(parsed))
    return padded[: len(minimum)] >= minimum


def _package_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _run(command: Sequence[str]) -> str | None:
    """Run a command and return its stdout, or None if it is unavailable."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        LOGGER.debug("command failed: %s (%s)", " ".join(command), error)
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def resolve_lab_dir() -> Path:
    """Locate the aurora-lab checkout, allowing an explicit override."""
    override = os.environ.get("RV_LAB_DIR")
    if override:
        return Path(override).expanduser()
    return DEFAULT_LAB_DIR


def check_python() -> Check:
    current = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] >= MINIMUM_PYTHON:
        return Check("python", Status.OK, current)
    minimum = f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"
    return Check("python", Status.FAIL, f"{current}, need >= {minimum}")


def check_pyyaml() -> Check:
    version = _package_version("PyYAML")
    if version is None:
        return Check("pyyaml", Status.FAIL, "not installed; run make install")
    if not _at_least(version, MINIMUM_PYYAML):
        return Check("pyyaml", Status.FAIL, f"{version}, need >= 6.0")
    return Check("pyyaml", Status.OK, version)


def check_dev_tool(name: str) -> Check:
    version = _package_version(name)
    if version is None:
        return Check(name, Status.WARN, "missing; run make install")
    return Check(name, Status.OK, version)


def check_kubectl() -> Check:
    path = shutil.which("kubectl")
    if path is None:
        return Check("kubectl", Status.WARN, "missing; LIVE unavailable, SNAPSHOT unaffected")
    version = "unknown"
    output = _run(["kubectl", "version", "--client", "-o", "json"])
    if output:
        try:
            version = str(json.loads(output)["clientVersion"]["gitVersion"])
        except (ValueError, KeyError, TypeError):
            version = "unknown"
    return Check("kubectl", Status.OK, f"{version} at {path}")


def check_kind() -> Check:
    path = shutil.which("kind")
    if path is None:
        return Check("kind", Status.WARN, "missing; LIVE unavailable, SNAPSHOT unaffected")
    output = _run(["kind", "version"])
    version = output.split()[1] if output and len(output.split()) > 1 else "unknown"
    return Check("kind", Status.OK, f"{version} at {path}")


def check_docker_daemon() -> Check:
    if shutil.which("docker") is None:
        return Check("docker daemon", Status.WARN, "docker missing; LIVE unavailable")
    output = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not output:
        return Check("docker daemon", Status.WARN, "not reachable; start Docker Desktop for LIVE")
    return Check("docker daemon", Status.OK, f"server {output}")


def check_lab(lab_dir: Path) -> Check:
    if not (lab_dir / "lab.sh").is_file():
        return Check("lab checkout", Status.WARN, f"not found at {lab_dir}")
    head = _run(["git", "-C", str(lab_dir), "rev-parse", "HEAD"])
    if head is None:
        return Check("lab checkout", Status.WARN, f"not a git checkout: {lab_dir}")
    if head != LAB_COMMIT:
        return Check(
            "lab checkout",
            Status.WARN,
            f"{head[:12]} at {lab_dir}, pinned {LAB_COMMIT[:12]}",
        )
    return Check("lab checkout", Status.OK, f"{head[:12]} at {lab_dir}")


def run_checks(lab_dir: Path | None = None) -> list[Check]:
    """Collect every check, sorted by name so output is stable."""
    target = lab_dir if lab_dir is not None else resolve_lab_dir()
    checks = [
        check_python(),
        check_pyyaml(),
        *(check_dev_tool(tool) for tool in DEV_TOOLS),
        check_kubectl(),
        check_kind(),
        check_docker_daemon(),
        check_lab(target),
    ]
    return sorted(checks, key=lambda check: check.name)


def live_available(checks: Sequence[Check]) -> bool:
    """LIVE needs the client, the cluster builder and a reachable daemon."""
    required = {"kubectl", "kind", "docker daemon"}
    return all(check.status is Status.OK for check in checks if check.name in required)


def render(checks: Sequence[Check]) -> str:
    """Format the checks as a fixed-width table plus a one-line summary."""
    width = max(len(check.name) for check in checks)
    lines = ["environment", ""]
    lines.extend(
        f"  {check.name:<{width}}  {check.status.value:<4}  {check.detail}" for check in checks
    )
    counts = Counter(check.status for check in checks)
    mode = "LIVE" if live_available(checks) else "SNAPSHOT"
    lines.extend(
        [
            "",
            f"summary: {counts[Status.OK]} OK, {counts[Status.WARN]} WARN, "
            f"{counts[Status.FAIL]} FAIL",
            f"available mode: {mode} (SNAPSHOT needs no cluster)",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m verifier.doctor",
        description="Report whether this machine can run the remediation verifier.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    checks = run_checks()
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2, sort_keys=True))
    else:
        print(render(checks))
    return 0 if all(check.status is not Status.FAIL for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

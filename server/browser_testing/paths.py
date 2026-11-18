"""Constrained filesystem locations for browser-test scenarios and evidence."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DIRECTORY = Path(__file__).resolve().parent
SCENARIO_DIRECTORY = (PACKAGE_DIRECTORY / "fixtures").resolve()
ARTIFACT_DIRECTORY = (PACKAGE_DIRECTORY / "artifacts").resolve()
ARTIFACT_FILENAMES = frozenset({"browser-evidence.png", "browser-result.json"})
AUTHORED_SCENARIO_FILE = SCENARIO_DIRECTORY / "avenue-rd-scenario.json"
RESULT_FILE = ARTIFACT_DIRECTORY / "browser-result.json"
RESULT_TEMPORARY_FILE = ARTIFACT_DIRECTORY / ".browser-result.json.tmp"
SCREENSHOT_FILE = ARTIFACT_DIRECTORY / "browser-evidence.png"


class PathAccessError(ValueError):
    """A requested file escaped its explicit browser-testing directory."""


def confined_path(directory: Path, requested: str | Path, *, label: str) -> Path:
    """Resolve *requested* and require it to remain below *directory*."""

    root = directory.resolve()
    supplied = Path(requested)
    candidate = supplied if supplied.is_absolute() else root / supplied
    resolved = candidate.resolve()
    try:
        common = Path(os.path.commonpath((str(root), str(resolved))))
    except ValueError as exc:
        raise PathAccessError(f"{label} must stay inside {root}") from exc
    if common != root or resolved == root:
        raise PathAccessError(f"{label} must stay inside {root}")
    return resolved


def scenario_file(requested: str | Path) -> Path:
    return confined_path(SCENARIO_DIRECTORY, requested, label="scenario file")


def artifact_file(filename: str) -> Path:
    if filename not in ARTIFACT_FILENAMES:
        raise PathAccessError("unsupported browser-test artifact filename")
    return confined_path(ARTIFACT_DIRECTORY, filename, label="artifact file")

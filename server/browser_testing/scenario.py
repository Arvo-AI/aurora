"""Strict, canonically hashed browser scenarios.

The scenario hash covers only the frozen behavior and its incident/baseline
identity. Authoring evidence is deliberately kept outside that hash so the
same checks can be replayed against many candidate commits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .paths import AUTHORED_SCENARIO_FILE, SCENARIO_DIRECTORY, scenario_file


SCHEMA_VERSION = 1
RUNNER_VERSION = "1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CHECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
LABEL_SELECTOR_RE = re.compile(
    r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,62}[A-Za-z0-9])?"
    r"(?:/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,62}[A-Za-z0-9])?)?="
    r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"
)


class ScenarioError(ValueError):
    """The scenario is malformed or no longer matches its frozen hash."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ScenarioError(f"{field} must be non-empty text up to {maximum} characters")
    return value.strip()


def _normalize_incident(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"id", "reference"}:
        raise ScenarioError("incident must contain only id and reference")
    return {
        "id": _text(value["id"], "incident.id", 128),
        "reference": _text(value["reference"], "incident.reference", 2048),
    }


def _normalize_baseline(value: Any) -> str:
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        raise ScenarioError("baselineCommit must be a full lowercase Git commit")
    return value


def _normalize_check(
    check: Any, index: int, seen_ids: set[str], target_ids: set[str]
) -> dict[str, Any]:
    if not isinstance(check, dict):
        raise ScenarioError(f"checks[{index}] has unsupported fields")
    fields = set(check)
    badge_fields = {"id", "rowText", "expectedBadge"}
    status_fields = {"id", "path", "expectedStatus"}
    targeted_status_fields = status_fields | {"target"}
    if fields not in (badge_fields, status_fields, targeted_status_fields):
        raise ScenarioError(f"checks[{index}] has unsupported fields")
    check_id = _text(check["id"], f"checks[{index}].id", 64)
    if not CHECK_ID_RE.fullmatch(check_id) or check_id in seen_ids:
        raise ScenarioError(f"checks[{index}].id is invalid or duplicated")
    seen_ids.add(check_id)
    if fields == badge_fields:
        return {
            "id": check_id,
            "rowText": _text(check["rowText"], f"checks[{index}].rowText", 256),
            "expectedBadge": _text(
                check["expectedBadge"], f"checks[{index}].expectedBadge", 256
            ),
        }

    path = _text(check["path"], f"checks[{index}].path", 2048)
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        raise ScenarioError(
            f"checks[{index}].path must be an origin-relative URL path without a fragment"
        )
    expected_status = check["expectedStatus"]
    if isinstance(expected_status, bool) or not isinstance(expected_status, int):
        raise ScenarioError(f"checks[{index}].expectedStatus must be an HTTP status")
    if not 100 <= expected_status <= 599:
        raise ScenarioError(f"checks[{index}].expectedStatus must be an HTTP status")
    result = {
        "id": check_id,
        "path": path,
        "expectedStatus": expected_status,
    }
    if fields == targeted_status_fields:
        target = _text(check["target"], f"checks[{index}].target", 64)
        if target not in target_ids:
            raise ScenarioError(f"checks[{index}].target does not name a scenario target")
        result["target"] = target
    return result


def _normalize_checks(value: Any, target_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ScenarioError("checks must contain between 1 and 16 entries")
    seen_ids: set[str] = set()
    return [
        _normalize_check(check, index, seen_ids, target_ids)
        for index, check in enumerate(value)
    ]


def _normalize_targets(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ScenarioError("targets must contain between 1 and 16 entries")
    seen_ids: set[str] = set()
    normalized = []
    for index, target in enumerate(value):
        if not isinstance(target, dict) or set(target) != {
            "id", "kind", "namespace", "selector", "port"
        }:
            raise ScenarioError(f"targets[{index}] has unsupported fields")
        target_id = _text(target["id"], f"targets[{index}].id", 64)
        if (
            not CHECK_ID_RE.fullmatch(target_id)
            or target_id == "preview"
            or target_id in seen_ids
        ):
            raise ScenarioError(f"targets[{index}].id is invalid or duplicated")
        seen_ids.add(target_id)
        if target["kind"] != "kubernetes-pod-label":
            raise ScenarioError(f"targets[{index}].kind is unsupported")
        namespace = _text(target["namespace"], f"targets[{index}].namespace", 63)
        selector = _text(target["selector"], f"targets[{index}].selector", 253)
        port = target["port"]
        if not NAMESPACE_RE.fullmatch(namespace):
            raise ScenarioError(f"targets[{index}].namespace is invalid")
        if not LABEL_SELECTOR_RE.fullmatch(selector):
            raise ScenarioError(f"targets[{index}].selector must be one exact label match")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ScenarioError(f"targets[{index}].port is invalid")
        normalized.append({
            "id": target_id,
            "kind": "kubernetes-pod-label",
            "namespace": namespace,
            "selector": selector,
            "port": port,
        })
    return normalized


def behavior_body(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schemaVersion",
        "runnerVersion",
        "name",
        "incident",
        "brief",
        "baselineCommit",
        "checks",
    }
    missing = required - value.keys()
    if missing:
        raise ScenarioError(f"scenario is missing: {', '.join(sorted(missing))}")

    if value["schemaVersion"] != SCHEMA_VERSION:
        raise ScenarioError("unsupported scenario schema version")
    if value["runnerVersion"] != RUNNER_VERSION:
        raise ScenarioError("unsupported browser runner version")

    targets = _normalize_targets(value.get("targets"))
    body = {
        "schemaVersion": SCHEMA_VERSION,
        "runnerVersion": RUNNER_VERSION,
        "name": _text(value["name"], "name", 256),
        "incident": _normalize_incident(value["incident"]),
        "brief": _text(value["brief"], "brief", 4096),
        "baselineCommit": _normalize_baseline(value["baselineCommit"]),
        "checks": _normalize_checks(value["checks"], {target["id"] for target in targets}),
    }
    if targets:
        body["targets"] = targets
    if len(canonical_json(body).encode("utf-8")) > 32768:
        raise ScenarioError("scenario exceeds 32 KiB")
    return body


def scenario_hash(value: dict[str, Any]) -> str:
    return digest(behavior_body(value))


def freeze_scenario(
    value: dict[str, Any], authoring: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = behavior_body(value)
    frozen = {**body, "scenarioHash": digest(body)}
    if authoring is not None:
        frozen["authoring"] = authoring
    return frozen


def validate_frozen(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError("scenario must be a JSON object")
    allowed = {
        "schemaVersion",
        "runnerVersion",
        "name",
        "incident",
        "brief",
        "baselineCommit",
        "checks",
        "targets",
        "scenarioHash",
        "authoring",
    }
    if set(value) - allowed:
        raise ScenarioError("scenario contains unsupported top-level fields")
    expected = value.get("scenarioHash")
    if not isinstance(expected, str) or not HASH_RE.fullmatch(expected):
        raise ScenarioError("scenarioHash must be a lowercase SHA-256 digest")
    frozen = freeze_scenario(value, value.get("authoring"))
    if frozen["scenarioHash"] != expected:
        raise ScenarioError("frozen scenario hash mismatch")
    return frozen


def load_scenario(path: str | Path) -> dict[str, Any]:
    target = scenario_file(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"cannot read scenario: {exc}") from exc
    return validate_frozen(value)


def save_scenario(value: dict[str, Any]) -> None:
    SCENARIO_DIRECTORY.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(SCENARIO_DIRECTORY, directory_flags)
    temporary_name = ".avenue-rd-scenario.json.tmp"
    final_name = AUTHORED_SCENARIO_FILE.name
    file_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        temporary_fd = os.open(temporary_name, file_flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(temporary_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(
            temporary_name,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)

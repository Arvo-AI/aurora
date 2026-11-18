"""Strict, canonically hashed browser scenarios.

The scenario hash covers only the frozen behavior and its incident/baseline
identity. Authoring evidence is deliberately kept outside that hash so the
same checks can be replayed against many candidate commits.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .paths import scenario_file


SCHEMA_VERSION = 1
RUNNER_VERSION = "1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CHECK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


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

    incident = value["incident"]
    if not isinstance(incident, dict) or set(incident) != {"id", "reference"}:
        raise ScenarioError("incident must contain only id and reference")
    incident_id = _text(incident["id"], "incident.id", 128)
    incident_reference = _text(incident["reference"], "incident.reference", 2048)

    baseline = value["baselineCommit"]
    if not isinstance(baseline, str) or not COMMIT_RE.fullmatch(baseline):
        raise ScenarioError("baselineCommit must be a full lowercase Git commit")

    checks = value["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
        raise ScenarioError("checks must contain between 1 and 16 entries")
    normalized_checks = []
    seen_ids: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or set(check) != {"id", "rowText", "expectedBadge"}:
            raise ScenarioError(f"checks[{index}] has unsupported fields")
        check_id = _text(check["id"], f"checks[{index}].id", 64)
        if not CHECK_ID_RE.fullmatch(check_id) or check_id in seen_ids:
            raise ScenarioError(f"checks[{index}].id is invalid or duplicated")
        seen_ids.add(check_id)
        normalized_checks.append(
            {
                "id": check_id,
                "rowText": _text(check["rowText"], f"checks[{index}].rowText", 256),
                "expectedBadge": _text(
                    check["expectedBadge"], f"checks[{index}].expectedBadge", 256
                ),
            }
        )

    body = {
        "schemaVersion": SCHEMA_VERSION,
        "runnerVersion": RUNNER_VERSION,
        "name": _text(value["name"], "name", 256),
        "incident": {"id": incident_id, "reference": incident_reference},
        "brief": _text(value["brief"], "brief", 4096),
        "baselineCommit": baseline,
        "checks": normalized_checks,
    }
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


def save_scenario(path: str | Path, value: dict[str, Any]) -> None:
    target = scenario_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    temporary.replace(target)

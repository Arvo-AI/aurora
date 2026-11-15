"""Load the frozen incident contract and expose its two views.

`load_contract` is the only place that reads the contract file. It validates
the document against `contracts/schema.json` with the local validator, then
builds immutable dataclasses, so a caller that holds a `Contract` never needs
the file again and cannot drift from it.

`agent_view` returns the alert and nothing else. The dictionary is built from
an `AgentView` that has one field, so there is no oracle to remove: the
separation is a property of the shape, and not a filter applied afterwards.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from verifier.config import ENCODING, SCHEMA_NAME, default_contract_path, schema_path_for
from verifier.model import (
    AgentView,
    Check,
    Contract,
    ContractError,
    Fault,
    Fix,
    Observation,
    OracleView,
    Source,
    Variant,
)
from verifier.schema import SchemaValidator

LOGGER = logging.getLogger(__name__)


def load_contract(path: Path | str | None = None) -> Contract:
    """Read, validate and type the contract at `path`, or at the configured one."""
    contract_path = Path(path) if path is not None else default_contract_path()
    schema_path = schema_path_for(contract_path)
    document = _read_yaml(contract_path)
    _validate(document, schema_path, contract_path)
    contract = _build(document)
    _check_links(contract)
    LOGGER.debug("loaded contract %s at %s", contract.scenario, contract.source.commit)
    return contract


def agent_view(contract: Contract) -> dict[str, Any]:
    """Return the only input an agent may see."""
    view = AgentView(alert=contract.agent.alert)
    return {"alert": view.alert}


def oracle_view(contract: Contract) -> dict[str, Any]:
    """Return the verifier-only data, with the contract's own shape."""
    oracle = contract.oracle
    return {
        "observation": {"window_seconds": oracle.observation.window_seconds},
        "baseline": {"expected_faults": list(oracle.expected_faults)},
        "faults": {name: _fault_to_dict(fault) for name, fault in oracle.faults.items()},
        "accepted_fixes": [_fix_to_dict(fix) for fix in oracle.fixes.values()],
        "variants": {
            name: {"kind": variant.kind, "fix": variant.fix}
            for name, variant in oracle.variants.items()
        },
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding=ENCODING)
    except OSError as error:
        raise ContractError(f"cannot read the contract at {path}: {error}") from error
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ContractError(f"the contract at {path} is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"the contract at {path} is not a mapping")
    return document


def _validate(document: dict[str, Any], schema_path: Path, contract_path: Path) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding=ENCODING))
    except OSError as error:
        raise ContractError(f"cannot read the schema at {schema_path}: {error}") from error
    errors = SchemaValidator(schema).errors(document)
    if errors:
        joined = "\n".join(f"  {error}" for error in errors)
        raise ContractError(f"{contract_path.name} does not match {SCHEMA_NAME}:\n{joined}")


def _build(document: dict[str, Any]) -> Contract:
    oracle_raw = _mapping(document, "oracle_view", "$")
    faults_raw = _mapping(oracle_raw, "faults", "$.oracle_view")
    fixes_raw = _sequence(oracle_raw, "accepted_fixes", "$.oracle_view")
    variants_raw = _mapping(oracle_raw, "variants", "$.oracle_view")
    observation_raw = _mapping(oracle_raw, "observation", "$.oracle_view")
    baseline_raw = _mapping(oracle_raw, "baseline", "$.oracle_view")
    agent_raw = _mapping(document, "agent_view", "$")

    faults = {
        name: _fault(name, _mapping(faults_raw, name, "$.oracle_view.faults"))
        for name in faults_raw
    }
    fixes = {
        fix.id: fix
        for fix in (
            _fix(item, f"$.oracle_view.accepted_fixes[{index}]")
            for index, item in enumerate(fixes_raw)
        )
    }
    variants = {
        name: _variant(name, _mapping(variants_raw, name, "$.oracle_view.variants"))
        for name in variants_raw
    }
    return Contract(
        scenario=_text(document, "scenario", "$"),
        title=_text(document, "title", "$"),
        source=_source(_mapping(document, "source", "$")),
        agent=AgentView(alert=_text(agent_raw, "alert", "$.agent_view")),
        oracle=OracleView(
            observation=Observation(
                window_seconds=_integer(observation_raw, "window_seconds", "$.oracle_view")
            ),
            expected_faults=tuple(
                _items_of(baseline_raw, "expected_faults", "$.oracle_view.baseline")
            ),
            faults=faults,
            fixes=fixes,
            variants=variants,
        ),
    )


def _source(raw: Mapping[str, Any]) -> Source:
    files = _mapping(raw, "files", "$.source")
    return Source(
        repo=_text(raw, "repo", "$.source"),
        url=_text(raw, "url", "$.source"),
        commit=_text(raw, "commit", "$.source"),
        commit_date=_text(raw, "commit_date", "$.source"),
        read_on=_text(raw, "read_on", "$.source"),
        fork_repo=_text(raw, "fork_repo", "$.source"),
        fork_commit=_text(raw, "fork_commit", "$.source"),
        files={name: _text(files, name, "$.source.files") for name in files},
    )


def _fault(name: str, raw: Mapping[str, Any]) -> Fault:
    where = f"$.oracle_view.faults.{name}"
    checks = _sequence(raw, "checks", where)
    return Fault(
        name=name,
        namespace=_text(raw, "namespace", where),
        checks=tuple(_check(item, f"{where}.checks[{index}]") for index, item in enumerate(checks)),
        deployment=_optional_text(raw, "deployment", where),
        container=_optional_text(raw, "container", where),
        selector=_optional_text(raw, "selector", where),
        service=_optional_text(raw, "service", where),
        endpoint_slice_selector=_optional_text(raw, "endpoint_slice_selector", where),
        selector_expected=_optional_text(raw, "selector_expected", where),
        http_route=_optional_text(raw, "http_route", where),
        http_method=_optional_text(raw, "http_method", where),
        expected_status=_optional_int(raw, "expected_status", where),
        expected_json=_optional_mapping(raw, "expected_json", where),
        requests=_optional_int(raw, "requests", where),
        request_interval_seconds=_optional_number(raw, "request_interval_seconds", where),
        request_timeout_seconds=_optional_number(raw, "request_timeout_seconds", where),
    )


def _check(raw: Any, where: str) -> Check:
    node = _as_mapping(raw, where)
    return Check(
        id=_text(node, "id", where),
        type=_text(node, "type", where),
        source=_text(node, "source", where),
        container=_optional_text(node, "container", where),
        pattern=_optional_text(node, "pattern", where),
        batch_size_source=_optional_text(node, "batch_size_source", where),
        min_occurrences=_optional_int(node, "min_occurrences", where),
    )


def _fix(raw: Any, where: str) -> Fix:
    node = _as_mapping(raw, where)
    return Fix(
        id=_text(node, "id", where),
        source=_text(node, "source", where),
        steps=tuple(_items_of(node, "steps", where)),
    )


def _variant(name: str, raw: Mapping[str, Any]) -> Variant:
    where = f"$.oracle_view.variants.{name}"
    return Variant(name=name, kind=_text(raw, "kind", where), fix=_optional_text(raw, "fix", where))


def _check_links(contract: Contract) -> None:
    oracle = contract.oracle
    unknown = sorted(set(oracle.expected_faults) - set(oracle.faults))
    if unknown:
        raise ContractError(f"baseline expects faults the contract does not define: {unknown}")
    for name, variant in oracle.variants.items():
        if variant.fix is not None and variant.fix not in oracle.fixes:
            raise ContractError(f"variant {name} points at an undefined fix: {variant.fix}")
    identifiers = [check.id for fault in oracle.faults.values() for check in fault.checks]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("two checks share an identifier")


def _fault_to_dict(fault: Fault) -> dict[str, Any]:
    data = asdict(fault)
    data.pop("name")
    data["checks"] = [
        {key: value for key, value in check.items() if value is not None}
        for check in data["checks"]
    ]
    return {key: value for key, value in data.items() if value is not None}


def _fix_to_dict(fix: Fix) -> dict[str, Any]:
    return {"id": fix.id, "source": fix.source, "steps": list(fix.steps)}


def _as_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{where}: expected a mapping")
    return value


def _mapping(node: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    return _as_mapping(node.get(key), f"{where}.{key}")


def _optional_mapping(node: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any] | None:
    value = node.get(key)
    return None if value is None else _as_mapping(value, f"{where}.{key}")


def _sequence(node: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = node.get(key)
    if not isinstance(value, list):
        raise ContractError(f"{where}.{key}: expected a list")
    return value


def _items_of(node: Mapping[str, Any], key: str, where: str) -> list[str]:
    return [_as_text(value, f"{where}.{key}") for value in _sequence(node, key, where)]


def _as_text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{where}: expected a string")
    return value


def _text(node: Mapping[str, Any], key: str, where: str) -> str:
    return _as_text(node.get(key), f"{where}.{key}")


def _optional_text(node: Mapping[str, Any], key: str, where: str) -> str | None:
    value = node.get(key)
    if value is None:
        return None
    return _as_text(value, f"{where}.{key}")


def _integer(node: Mapping[str, Any], key: str, where: str) -> int:
    value = node.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{where}.{key}: expected an integer")
    return value


def _optional_int(node: Mapping[str, Any], key: str, where: str) -> int | None:
    value = node.get(key)
    if value is None:
        return None
    return _integer(node, key, where)


def _optional_number(node: Mapping[str, Any], key: str, where: str) -> float | None:
    value = node.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ContractError(f"{where}.{key}: expected a number")
    return float(value)

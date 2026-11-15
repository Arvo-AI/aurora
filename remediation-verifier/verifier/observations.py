"""Normalise LIVE and SNAPSHOT observations into one schema.

A snapshot is a JSON document shaped like the dataclasses in `verifier.model`.
Loading one never touches Kubernetes: the live collector lives in
`verifier.live`, so the snapshot path cannot call a cluster even by accident.

R11 is enforced here. Structure is validated against
`contracts/snapshot_schema.json`: a section of the wrong JSON type is a
malformed document and raises. Metadata values are different. A capture with a
naive timestamp, or a provenance label that is not LIVE, still loads with
`missing_metadata` filled in, because R11 turns an unusable capture into
INCONCLUSIVE rather than aborting the run. `require_complete` is the strict
door used by capture and by tests, so an incomplete dump is never published as
evidence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from verifier import parse
from verifier.config import ENCODING, snapshot_schema_path
from verifier.model import (
    ContainerStatus,
    EndpointAddress,
    EndpointSlice,
    HttpProbe,
    Logs,
    Meta,
    ObservationError,
    Observations,
    Pod,
    Service,
)
from verifier.schema import SchemaValidator

LOGGER = logging.getLogger(__name__)

PROVENANCE_LIVE = "LIVE"
MODE_LIVE = "LIVE"
MODE_SNAPSHOT = "SNAPSHOT"

REQUIRED_META = ("lab_commit", "variant", "captured_at", "provenance")


@lru_cache(maxsize=1)
def snapshot_validator() -> SchemaValidator:
    """Return the validator for contracts/snapshot_schema.json, loaded once."""
    try:
        schema = json.loads(snapshot_schema_path().read_text(encoding=ENCODING))
    except OSError as error:
        raise ObservationError(f"cannot read the snapshot schema: {error}") from error
    return SchemaValidator(schema)


def from_snapshot(source: Path | str | Mapping[str, Any]) -> Observations:
    """Load a snapshot document and report what it is missing.

    A path is read as JSON. A mapping is used as-is, which is how the live
    collector hands over its document without writing a file first.
    """
    document = _document(source)
    _reject_schema_errors(document)
    missing: list[str] = []
    observations = Observations(
        meta=_meta(document, missing),
        pods=_pods(document, missing),
        services=_services(document, missing),
        endpoints=_endpoints(document, missing),
        http=_http(document, missing),
        logs=_logs(document, missing),
        batch_size=_batch_size(document, missing),
        timing={},
        missing_metadata=tuple(dict.fromkeys(missing)),
    )
    LOGGER.debug(
        "loaded snapshot %s with %d missing fields", observations.meta.variant, len(missing)
    )
    return observations


def to_snapshot(observations: Observations, path: Path | str) -> Path:
    """Write a deterministic JSON snapshot and return the path it wrote."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot_document(observations), indent=2, sort_keys=True)
    target.write_text(f"{text}\n", encoding=ENCODING)
    return target


def snapshot_document(observations: Observations) -> dict[str, Any]:
    """Return the snapshot shape as JSON-ready data. Timing stays out: it is volatile."""
    document = {
        "meta": _as_dict(observations.meta),
        "pods": [_as_dict(pod) for pod in observations.pods],
        "services": [_as_dict(service) for service in observations.services],
        "endpoints": [_as_dict(slice_) for slice_ in observations.endpoints],
        "http": [_as_dict(probe) for probe in observations.http],
        "logs": _as_dict(observations.logs),
        "worker": {"batch_size": observations.batch_size},
    }
    return cast(dict[str, Any], _json_ready(document))


def _json_ready(value: Any) -> Any:
    """Turn tuples into lists, so the document reloads without a JSON round trip."""
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return asdict(value)


def _document(source: Path | str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    path = Path(source)
    try:
        text = path.read_text(encoding=ENCODING)
    except OSError as error:
        raise ObservationError(f"cannot read the snapshot at {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ObservationError(f"the snapshot at {path} is not {ENCODING}: {error}") from error
    try:
        document = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise ObservationError(f"the snapshot at {path} is not valid JSON: {error}") from error
    if not isinstance(document, Mapping):
        raise ObservationError(f"the snapshot at {path} is not an object")
    return document


def _reject_constant(value: str) -> Any:
    raise ObservationError(f"the snapshot uses {value}, which is not valid JSON")


def _reject_schema_errors(document: Mapping[str, Any]) -> None:
    errors = snapshot_validator().errors(document)
    if errors:
        joined = "\n".join(f"  {error}" for error in errors)
        raise ObservationError(f"the snapshot does not match the snapshot schema:\n{joined}")


def _meta(document: Mapping[str, Any], missing: list[str]) -> Meta:
    if "meta" not in document:
        missing.extend(REQUIRED_META)
        return Meta(lab_commit="", variant="", mode=MODE_SNAPSHOT, captured_at="", provenance="")

    raw = _as_mapping(document["meta"], "$.meta")
    lab_commit = _optional_text(raw.get("lab_commit"), "$.meta.lab_commit")
    variant = _optional_text(raw.get("variant"), "$.meta.variant")
    captured_at = _optional_text(raw.get("captured_at"), "$.meta.captured_at")
    provenance = _optional_text(raw.get("provenance"), "$.meta.provenance")

    if not lab_commit:
        missing.append("lab_commit")
    if not variant:
        missing.append("variant")
    if not _is_timestamp(captured_at):
        missing.append("captured_at")
    if provenance != PROVENANCE_LIVE:
        missing.append("provenance")

    return Meta(
        lab_commit=lab_commit or "",
        variant=variant or "",
        mode=MODE_SNAPSHOT,
        captured_at=captured_at or "",
        provenance=provenance or "",
    )


def _pods(document: Mapping[str, Any], missing: list[str]) -> tuple[Pod, ...]:
    nodes = _section(document, "pods", missing)
    pods = []
    for index, node in enumerate(nodes):
        if "restart_count" not in node:
            missing.append("restart_counts")
        pods.append(_pod(node, f"$.pods[{index}]"))
    return tuple(pods)


def _pod(node: Mapping[str, Any], where: str) -> Pod:
    containers = _as_list(node.get("containers", []), f"{where}.containers")
    return Pod(
        name=_text(node, "name", where),
        uid=_optional_text(node.get("uid"), f"{where}.uid") or "",
        phase=_optional_text(node.get("phase"), f"{where}.phase") or "Unknown",
        ready=_flag(node.get("ready"), f"{where}.ready"),
        restart_count=_restart_count(node, where),
        waiting_reason=_optional_text(node.get("waiting_reason"), f"{where}.waiting_reason"),
        last_terminated_reason=_optional_text(
            node.get("last_terminated_reason"), f"{where}.last_terminated_reason"
        ),
        last_exit_code=_optional_int(node.get("last_exit_code"), f"{where}.last_exit_code"),
        containers=tuple(
            _container(item, f"{where}.containers[{position}]")
            for position, item in enumerate(containers)
        ),
    )


def _container(value: Any, where: str) -> ContainerStatus:
    node = _as_mapping(value, where)
    return ContainerStatus(
        name=_text(node, "name", where),
        ready=_flag(node.get("ready"), f"{where}.ready"),
        restart_count=_restart_count(node, where),
        waiting_reason=_optional_text(node.get("waiting_reason"), f"{where}.waiting_reason"),
        last_terminated_reason=_optional_text(
            node.get("last_terminated_reason"), f"{where}.last_terminated_reason"
        ),
        last_exit_code=_optional_int(node.get("last_exit_code"), f"{where}.last_exit_code"),
    )


def _services(document: Mapping[str, Any], missing: list[str]) -> tuple[Service, ...]:
    services = []
    for index, node in enumerate(_section(document, "services", missing)):
        where = f"$.services[{index}]"
        selector = _as_mapping(node.get("selector", {}), f"{where}.selector")
        services.append(
            Service(
                name=_text(node, "name", where),
                selector={
                    key: parse.as_text(value, f"{where}.selector.{key}", ObservationError)
                    for key, value in selector.items()
                },
                ports=tuple(
                    parse.int_list(node.get("ports", []), f"{where}.ports", ObservationError)
                ),
                cluster_ip=_optional_text(node.get("cluster_ip"), f"{where}.cluster_ip") or "",
            )
        )
    return tuple(services)


def _endpoints(document: Mapping[str, Any], missing: list[str]) -> tuple[EndpointSlice, ...]:
    slices = []
    for index, node in enumerate(_section(document, "endpoints", missing)):
        where = f"$.endpoints[{index}]"
        addresses = _as_list(node.get("addresses", []), f"{where}.addresses")
        slices.append(
            EndpointSlice(
                name=_text(node, "name", where),
                addresses=tuple(
                    _endpoint_address(item, f"{where}.addresses[{position}]")
                    for position, item in enumerate(addresses)
                ),
            )
        )
    return tuple(slices)


def _endpoint_address(value: Any, where: str) -> EndpointAddress:
    node = _as_mapping(value, where)
    return EndpointAddress(
        address=_text(node, "address", where),
        ready=_flag(node.get("ready"), f"{where}.ready"),
    )


def _http(document: Mapping[str, Any], missing: list[str]) -> tuple[HttpProbe, ...]:
    probes = []
    for index, node in enumerate(_section(document, "http", missing)):
        where = f"$.http[{index}]"
        probes.append(
            HttpProbe(
                route=_text(node, "route", where),
                status=_http_status(node.get("status"), f"{where}.status"),
                body=_optional_text(node.get("body"), f"{where}.body"),
                duration_seconds=parse.optional_number(
                    node.get("duration_seconds"),
                    f"{where}.duration_seconds",
                    ObservationError,
                ),
                error=_optional_text(node.get("error"), f"{where}.error"),
            )
        )
    return tuple(probes)


def _logs(document: Mapping[str, Any], missing: list[str]) -> Logs:
    if "logs" not in document:
        missing.extend(("logs", "worker_logs"))
        return Logs()
    raw = _as_mapping(document["logs"], "$.logs")
    if "worker" not in raw:
        missing.append("worker_logs")
    return Logs(
        worker=tuple(parse.text_list(raw.get("worker", []), "$.logs.worker", ObservationError)),
        loadgen=tuple(parse.text_list(raw.get("loadgen", []), "$.logs.loadgen", ObservationError)),
    )


def _batch_size(document: Mapping[str, Any], missing: list[str]) -> int | None:
    """Read the worker's current BATCH_SIZE, or report that it was not captured."""
    if "worker" not in document:
        missing.append("batch_size")
        return None
    raw = _as_mapping(document["worker"], "$.worker")
    value = _optional_int(raw.get("batch_size"), "$.worker.batch_size")
    if value is None or value <= 0:
        missing.append("batch_size")
        return None
    return value


def _section(document: Mapping[str, Any], key: str, missing: list[str]) -> list[Mapping[str, Any]]:
    if key not in document:
        missing.append(key)
        return []
    items = _as_list(document[key], f"$.{key}")
    return [_as_mapping(item, f"$.{key}[{index}]") for index, item in enumerate(items)]


def _is_timestamp(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _flag(value: Any, where: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ObservationError(f"{where}: expected a boolean")
    return value


def _restart_count(node: Mapping[str, Any], where: str) -> int:
    count = _optional_int(node.get("restart_count"), f"{where}.restart_count") or 0
    if count < 0:
        raise ObservationError(f"{where}.restart_count: expected a non-negative integer")
    return count


def _http_status(value: Any, where: str) -> int | None:
    status = _optional_int(value, where)
    if status is not None and not 100 <= status <= 599:
        raise ObservationError(f"{where}: expected an HTTP status between 100 and 599")
    return status


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    return parse.as_mapping(value, where, ObservationError)


def _as_list(value: Any, where: str) -> list[Any]:
    return parse.as_list(value, where, ObservationError)


def _text(node: Mapping[str, Any], key: str, where: str) -> str:
    return parse.required(node, key, where, ObservationError)


def _optional_text(value: Any, where: str) -> str | None:
    return parse.optional_text(value, where, ObservationError)


def _optional_int(value: Any, where: str) -> int | None:
    return parse.optional_int(value, where, ObservationError)

"""Evidence normalization for the evidence-verified RCA replay.

An evidence bundle is a fixed directory of incident artifacts shipped with the
repository. Normalization turns those files into numbered records with a stable
server-assigned ID, a provenance locator, and a parsed UTC timestamp.

Two invariants this module is responsible for:

1. **IDs are assigned here, never by the model.** A finding may only cite an ID
   that this module produced; nothing downstream can invent one.
2. **A timestamp is never guessed.** ``parse_ts`` accepts only the formats the
   bundle actually uses and returns ``None`` for anything else, which surfaces
   as a warning and leaves the record's ``at`` empty. An unknown offset is not
   silently treated as UTC.

Record types are deliberately finer than "file". A deployment produces both a
``deploy_event`` ("deploy-4471 ran at 14:32:08") and one ``deploy_change`` per
changed field ("db.pool.maxPoolSize: 50 -> 10"). Keeping them apart is what lets
the validator tell "a deploy happened" from "this specific value changed".
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: Only bundles named here can be run. A request may pick a bundle by name; it
#: may never supply a path, so there is no way to make the server read an
#: arbitrary file.
BUNDLE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

BUNDLE_NAMES = ("checkout-api",)

#: Bundle name -> directory, resolved once at import time.
#:
#: The request value is used only as a dictionary key. Building the path from it
#: (``os.path.join(BUNDLE_ROOT, name)``) would let a caller learn whether a path
#: exists by observing which error comes back, even with a whitelist in front of
#: it. Here every path below is a module constant.
_BUNDLE_DIRS = {
    "checkout-api": os.path.join(BUNDLE_ROOT, "checkout-api"),
}

RECORD_ALERT = "alert"
RECORD_METRIC = "metric"
RECORD_LOG = "log"
RECORD_DEPLOY_EVENT = "deploy_event"
RECORD_DEPLOY_CHANGE = "deploy_change"
RECORD_RUNBOOK = "runbook"


def parse_ts(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp that carries a time component.

    ``datetime.fromisoformat`` (3.11+) understands both ``Z`` and explicit
    offsets, so no hand-rolled pattern is needed. A date with no time component
    is rejected: a record that says only "2026-11-06" has no place on a timeline.
    A timestamp with no offset is treated as UTC only because every artifact in
    these bundles is emitted by systems configured to UTC and says so; anything
    unparseable returns ``None`` rather than being assumed.
    """
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if "T" not in candidate and " " not in candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class EvidenceRecord:
    id: int
    source: str
    locator: str
    record_type: str
    at: Optional[str]
    at_raw: Optional[str]
    content: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceBundle:
    name: str
    records: List[EvidenceRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def by_id(self, record_id: int) -> Optional[EvidenceRecord]:
        for record in self.records:
            if record.id == record_id:
                return record
        return None

    def to_prompt_block(self) -> str:
        lines = []
        for record in self.records:
            lines.append(
                f"[{record.id}] {record.record_type} | {record.at or 'TIME-UNKNOWN'} | "
                f"{record.source}:{record.locator}\n    {record.content}"
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "records": [r.to_dict() for r in self.records],
            "warnings": self.warnings,
        }


def _append(
    bundle: EvidenceBundle,
    *,
    source: str,
    locator: str,
    record_type: str,
    at_raw: Optional[str],
    content: str,
) -> None:
    parsed = parse_ts(at_raw)
    if at_raw and parsed is None:
        bundle.warnings.append(f"{source}:{locator} has an unparseable timestamp {at_raw!r}")
    bundle.records.append(
        EvidenceRecord(
            id=len(bundle.records) + 1,
            source=source,
            locator=locator,
            record_type=record_type,
            at=parsed.isoformat().replace("+00:00", "Z") if parsed else None,
            at_raw=at_raw,
            content=content,
        )
    )


def _load_alert(bundle: EvidenceBundle, path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    source = os.path.basename(path)
    _append(
        bundle,
        source=source,
        locator="$.fired_at",
        record_type=RECORD_ALERT,
        at_raw=data.get("fired_at"),
        content=(
            f"alert {data.get('alert_id')} {data.get('severity')} "
            f"{data.get('service')}/{data.get('environment')}: {data.get('summary')} "
            f"(observed {data.get('observed_value')}, threshold {data.get('threshold')})"
        ),
    )


def _load_metrics(bundle: EvidenceBundle, path: str) -> None:
    source = os.path.basename(path)
    with open(path, encoding="utf-8") as handle:
        reader = csv.DictReader(io.StringIO(handle.read()))
        for line_no, row in enumerate(reader, start=2):
            _append(
                bundle,
                source=source,
                locator=f"line {line_no}",
                record_type=RECORD_METRIC,
                at_raw=row.get("timestamp"),
                content=(
                    f"{row.get('service')} {row.get('metric')}={row.get('value')} "
                    f"{row.get('unit')}"
                ),
            )


def _load_log(bundle: EvidenceBundle, path: str) -> None:
    source = os.path.basename(path)
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            head, _, rest = line.partition(" ")
            _append(
                bundle,
                source=source,
                locator=f"line {line_no}",
                record_type=RECORD_LOG,
                at_raw=head,
                content=rest.strip(),
            )


def _load_deployment(bundle: EvidenceBundle, path: str) -> None:
    source = os.path.basename(path)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    _append(
        bundle,
        source=source,
        locator="$.started_at",
        record_type=RECORD_DEPLOY_EVENT,
        at_raw=data.get("started_at"),
        content=(
            f"deploy {data.get('deploy_id')} commit {data.get('commit')} "
            f"{data.get('service')}/{data.get('environment')} "
            f"rollout={data.get('rollout')} completed_at={data.get('completed_at')}"
        ),
    )
    for index, change in enumerate(data.get("changes") or []):
        _append(
            bundle,
            source=source,
            locator=f"$.changes[{index}]",
            record_type=RECORD_DEPLOY_CHANGE,
            at_raw=data.get("started_at"),
            content=(
                f"{change.get('file')} {change.get('field')}: "
                f"{change.get('from')} -> {change.get('to')}"
            ),
        )


def _load_runbook(bundle: EvidenceBundle, path: str) -> None:
    source = os.path.basename(path)
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            _append(
                bundle,
                source=source,
                locator=f"line {line_no}",
                record_type=RECORD_RUNBOOK,
                at_raw=None,
                content=line.strip(),
            )


_LOADERS = {
    "alert.json": _load_alert,
    "metrics.csv": _load_metrics,
    "application.log": _load_log,
    "deployment.json": _load_deployment,
    "runbook.md": _load_runbook,
}


def normalize_bundle(name: str) -> EvidenceBundle:
    """Load and time-order the named bundle.

    ``name`` selects from :data:`_BUNDLE_DIRS`; it never becomes part of a path.
    An unknown name raises the same ``ValueError`` regardless of whether a
    directory with that name happens to exist, so the response cannot be used
    to probe the filesystem.
    """
    directory = _BUNDLE_DIRS.get(name) if isinstance(name, str) else None
    if directory is None:
        raise ValueError("unknown evidence bundle")

    bundle = EvidenceBundle(name=name)
    for filename, loader in _LOADERS.items():
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            bundle.warnings.append(f"missing evidence file: {filename}")
            continue
        loader(bundle, path)

    if not bundle.records:
        raise ValueError("evidence bundle produced no records")

    # Sort before numbering so the IDs a model sees ascend with the timeline.
    # Records without a timestamp (the runbook) sort last: the runbook explains
    # how to respond, not when something happened, so it must not interleave.
    bundle.records.sort(key=lambda r: (r.at is None, r.at or "", r.source, r.locator))
    for position, record in enumerate(bundle.records, start=1):
        record.id = position
    return bundle


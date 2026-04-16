"""
Datadog silence drift detection.

Combines three read-only Datadog sources (v2 downtimes, per-monitor downtime
matches, v1 monitor.silenced legacy field) with the HCL index to produce the
set of silences that exist in Datadog but have no Terraform counterpart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional

from routes.datadog.datadog_routes import (
    DatadogAPIError,
    DatadogClient,
    _build_client_from_creds,
    _get_stored_datadog_credentials,
)
from services.terraform.datadog_hcl_indexer import (
    IndexedResource,
    MatchConfidence,
    load_index,
    match_resources_to_monitor,
)

logger = logging.getLogger(__name__)

SilenceSource = Literal["downtime_v2", "monitor_silenced_legacy"]


@dataclass
class Silence:
    source: SilenceSource
    monitor_id: Optional[int]
    monitor_name: Optional[str]
    monitor_query: Optional[str]
    scope: Optional[str]
    downtime_id: Optional[str]
    muted_since: Optional[str]
    muted_until: Optional[str]
    message: Optional[str] = None


@dataclass
class DriftRow:
    silence: Silence
    matched_tf_resource: Optional[IndexedResource]
    tf_match_confidence: MatchConfidence
    is_already_codified: bool
    reason_if_codified: str = ""


@dataclass
class DriftReport:
    drifted: List[DriftRow] = field(default_factory=list)
    codified: List[DriftRow] = field(default_factory=list)
    monitors_total: int = 0
    silences_total: int = 0
    errors: List[str] = field(default_factory=list)
    indexed_resources: List[IndexedResource] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_silence_inventory(user_id: str) -> List[Silence]:
    """Fetch the current set of silences from Datadog (read-only)."""
    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        raise DatadogAPIError("Datadog is not connected for this user")
    client = _build_client_from_creds(creds)
    if client is None:
        raise DatadogAPIError("Failed to construct Datadog client")

    silences: List[Silence] = []
    silences.extend(_list_active_downtimes(client))
    silences.extend(_list_legacy_silenced_monitors(client))
    # Dedupe on (monitor_id, scope, source) to avoid duplicates when the legacy
    # field and a v2 downtime cover the same scope.
    seen = set()
    deduped: List[Silence] = []
    for s in silences:
        key = (s.source, s.monitor_id, s.scope or "", s.downtime_id or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped


def compute_drift(
    user_id: str,
    repo_full_name: Optional[str] = None,
    monitor_filter: Optional[str] = None,
    scope_filter: Optional[str] = None,
) -> DriftReport:
    """Build the drift report against the current HCL index.

    Drift = a silence in Datadog with no corresponding Terraform block.
    """
    report = DriftReport()
    try:
        inventory = build_silence_inventory(user_id)
    except DatadogAPIError as exc:
        report.errors.append(str(exc))
        return report

    report.silences_total = len(inventory)

    index = load_index(user_id, repo_full_name)
    report.indexed_resources = index
    monitor_index = [r for r in index if r.resource_type == "datadog_monitor"]
    schedule_index = [r for r in index if r.resource_type == "datadog_downtime_schedule"]
    legacy_downtime_index = [r for r in index if r.resource_type == "datadog_downtime"]

    monitor_ids_seen = set()
    for silence in inventory:
        if scope_filter and (silence.scope or "") != scope_filter:
            continue
        if monitor_filter and monitor_filter.lower() not in (silence.monitor_name or "").lower():
            continue
        monitor_ids_seen.add(silence.monitor_id)

        matched, confidence = match_resources_to_monitor(
            monitor_index, silence.monitor_name or "", silence.monitor_query
        )

        # Is this silence already codified?
        codified, reason = _is_silence_codified(
            silence, matched, schedule_index, legacy_downtime_index
        )
        row = DriftRow(
            silence=silence,
            matched_tf_resource=matched,
            tf_match_confidence=confidence,
            is_already_codified=codified,
            reason_if_codified=reason,
        )
        if codified:
            report.codified.append(row)
        else:
            report.drifted.append(row)

    report.monitors_total = len(monitor_ids_seen)
    _persist_drift_snapshot(user_id, report)
    return report


def drift_row_to_dict(row: DriftRow) -> Dict[str, Any]:
    tf = row.matched_tf_resource
    return {
        "monitor_id": row.silence.monitor_id,
        "monitor_name": row.silence.monitor_name,
        "scope": row.silence.scope,
        "muted_since": row.silence.muted_since,
        "muted_until": row.silence.muted_until,
        "source": row.silence.source,
        "downtime_id": row.silence.downtime_id,
        "original_message": row.silence.message,
        "matched_tf_file": tf.file_path if tf else None,
        "matched_tf_address": tf.resource_address if tf else None,
        "tf_match_confidence": row.tf_match_confidence,
        "is_already_codified": row.is_already_codified,
        "reason_if_codified": row.reason_if_codified,
    }


# ---------------------------------------------------------------------------
# Datadog fetchers (read-only)
# ---------------------------------------------------------------------------


def _list_active_downtimes(client: DatadogClient) -> List[Silence]:
    try:
        payload = client.list_downtimes(current_only=True, include_monitor=True)
    except DatadogAPIError as exc:
        logger.warning("[DRIFT] list_downtimes failed: %s", exc)
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    included = payload.get("included") if isinstance(payload, dict) else None
    monitor_by_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(included, list):
        for item in included:
            if isinstance(item, dict) and item.get("type") == "monitor":
                monitor_by_id[str(item.get("id"))] = item.get("attributes") or {}

    silences: List[Silence] = []
    if not isinstance(data, list):
        return silences

    for item in data:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes") or {}
        relationships = item.get("relationships") or {}
        monitor_rel = (relationships.get("monitor") or {}).get("data") or {}
        monitor_ref_id = monitor_rel.get("id")
        monitor_attr = monitor_by_id.get(str(monitor_ref_id), {}) if monitor_ref_id else {}

        monitor_identifier = attributes.get("monitor_identifier") or {}
        monitor_id = None
        if isinstance(monitor_identifier, dict):
            monitor_id = monitor_identifier.get("monitor_id")
        if monitor_id is None and monitor_ref_id:
            try:
                monitor_id = int(monitor_ref_id)
            except (TypeError, ValueError):
                monitor_id = None

        schedule = attributes.get("schedule") or {}
        start = schedule.get("start") if isinstance(schedule, dict) else None
        end = schedule.get("end") if isinstance(schedule, dict) else None

        silences.append(
            Silence(
                source="downtime_v2",
                monitor_id=monitor_id,
                monitor_name=monitor_attr.get("name"),
                monitor_query=monitor_attr.get("query"),
                scope=_stringify_scope(attributes.get("scope")),
                downtime_id=item.get("id"),
                muted_since=start,
                muted_until=end,
                message=attributes.get("message"),
            )
        )
    return silences


def _list_legacy_silenced_monitors(client: DatadogClient) -> List[Silence]:
    """Catch customers still using the deprecated monitor.silenced attribute."""
    silences: List[Silence] = []
    page = 0
    while True:
        try:
            payload = client.list_monitors(
                {"page": page, "page_size": 100, "group_states": "all", "with_downtimes": "false"}
            )
        except DatadogAPIError as exc:
            logger.warning("[DRIFT] list_monitors failed: %s", exc)
            break
        if not isinstance(payload, list) or not payload:
            break
        for monitor in payload:
            if not isinstance(monitor, dict):
                continue
            options = monitor.get("options") or {}
            silenced_map = options.get("silenced") if isinstance(options, dict) else None
            if not isinstance(silenced_map, dict) or not silenced_map:
                continue
            for scope_key, _end_ts in silenced_map.items():
                silences.append(
                    Silence(
                        source="monitor_silenced_legacy",
                        monitor_id=monitor.get("id"),
                        monitor_name=monitor.get("name"),
                        monitor_query=monitor.get("query"),
                        scope=scope_key or "*",
                        downtime_id=None,
                        muted_since=None,
                        muted_until=None,
                        message=None,
                    )
                )
        if len(payload) < 100:
            break
        page += 1
    return silences


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _is_silence_codified(
    silence: Silence,
    matched_monitor: Optional[IndexedResource],
    schedule_index: Iterable[IndexedResource],
    legacy_downtime_index: Iterable[IndexedResource],
) -> tuple[bool, str]:
    """Does the repo already contain a block that covers this silence?"""
    scope = (silence.scope or "").strip()

    if matched_monitor and matched_monitor.silenced_inline:
        inline = matched_monitor.silenced_inline
        if isinstance(inline, dict):
            if "*" in inline or (scope and scope in inline):
                return True, f"already in {matched_monitor.file_path} options.silenced"

    def _targets_match(block: IndexedResource) -> bool:
        if not matched_monitor:
            return False
        ref = block.downtime_monitor_ref or ""
        if not isinstance(ref, str):
            ref = str(ref)
        return (
            matched_monitor.resource_address in ref
            or matched_monitor.resource_address.split(".")[-1] in ref
        )

    for block in schedule_index:
        block_scope = (block.scope or "").strip()
        if _targets_match(block) and (not scope or not block_scope or block_scope == scope):
            return True, f"already in {block.file_path} ({block.resource_address})"

    for block in legacy_downtime_index:
        block_scope = (block.scope or "").strip()
        if _targets_match(block) and (not scope or not block_scope or block_scope == scope):
            return True, f"already in {block.file_path} ({block.resource_address})"

    return False, ""


def _stringify_scope(scope: Any) -> Optional[str]:
    if scope is None:
        return None
    if isinstance(scope, list):
        return ",".join(str(s) for s in scope)
    return str(scope)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_drift_snapshot(user_id: str, report: DriftReport) -> None:
    from psycopg2.extras import execute_values
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM datadog_silence_drift WHERE user_id = %s", (user_id,))
            if report.drifted:
                rows = [
                    (
                        user_id,
                        row.silence.monitor_id,
                        row.silence.monitor_name,
                        row.silence.scope,
                        row.silence.downtime_id,
                        _parse_iso(row.silence.muted_since),
                        row.silence.source,
                        row.matched_tf_resource.file_path if row.matched_tf_resource else None,
                        row.tf_match_confidence,
                        row.silence.message,
                    )
                    for row in report.drifted
                ]
                execute_values(
                    cur,
                    """
                    INSERT INTO datadog_silence_drift
                    (user_id, monitor_id, monitor_name, scope, downtime_id, muted_since,
                     source, matched_tf_file, tf_match_confidence, original_message)
                    VALUES %s
                    ON CONFLICT (user_id, monitor_id, scope, source) DO UPDATE
                    SET monitor_name = EXCLUDED.monitor_name,
                        downtime_id = EXCLUDED.downtime_id,
                        muted_since = EXCLUDED.muted_since,
                        matched_tf_file = EXCLUDED.matched_tf_file,
                        tf_match_confidence = EXCLUDED.tf_match_confidence,
                        original_message = EXCLUDED.original_message,
                        computed_at = CURRENT_TIMESTAMP
                    """,
                    rows,
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DRIFT] snapshot persist failed: %s", exc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None

"""Celery tasks for incident.io webhook event processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import apply_correlation_outcome

logger = logging.getLogger(__name__)

# Org priority catalog is cached (name → rank) so we rank custom priorities by
# the org's own ranking without an API call per alert; short TTL keeps it fresh.
_ORG_SEVERITY_CACHE_TTL_SECONDS = 3600
# API keys lacking "View data" scope get 403 forever — cache a "denied" marker
# to stop hammering, but re-check every 5 min so widened scopes self-heal.
_ORG_SEVERITY_DENIED_TTL_SECONDS = 300
_ORG_SEVERITY_DENIED_MARKER = "__denied__"

# Fixed severity buckets ordered least → most severe. Used to compare a minimum
# threshold against an alert's severity when both are recognized fixed buckets
# (i.e. we can't or don't need to consult the org's custom priority catalog).
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _get_org_severity_ranks(user_id: str) -> Dict[str, int]:
    """Return the org's alert-priority catalog as {lowercase_name: rank}.

    Alerts are categorized by *priority*, which incident.io models as a ranked
    Catalog type ("AlertPriority") with entries like "Urgent"/"In-hours". This
    is a separate taxonomy from incident severities. Higher rank = more urgent,
    matching this module's "higher rank = more severe" convention (no inversion
    needed).

    Cached in Redis (per-user) to avoid an API call on every alert; fails open
    (returns {}) on any error so the caller falls back to name-based mapping
    rather than dropping the alert.

    Reading the catalog requires the API key's "View data" (viewer) scope. A
    key without it gets a 403 — a *permanent* condition for that key — so we
    cache a "denied" marker (short TTL, re-checked in case scopes change)
    rather than retrying on every alert. Transient errors (timeout/5xx) are
    NOT cached, so the next alert retries immediately.
    """
    import json as _json

    cache_key = f"incidentio:severity_ranks:{user_id}"

    # Serve from cache when available — avoids an API call per alert.
    try:
        from utils.cache.redis_client import get_redis_client
        rc = get_redis_client()
        if rc is not None:
            cached = rc.get(cache_key)
            if cached is not None:
                # Key is known to lack the required scope — skip the API call.
                if cached == _ORG_SEVERITY_DENIED_MARKER:
                    return {}
                parsed = _json.loads(cached)
                if isinstance(parsed, dict):
                    return {str(k): int(v) for k, v in parsed.items()}
    except Exception:
        logger.debug("[INCIDENTIO] Severity-rank cache read failed", exc_info=True)

    # Cache miss (or no Redis) — fetch the org's alert priorities from the API.
    from routes.incidentio.incidentio_routes import IncidentioAPIError

    ranks: Dict[str, int] = {}
    denied = False
    try:
        from utils.auth.token_management import get_token_data
        from routes.incidentio.incidentio_routes import IncidentioClient

        creds = get_token_data(user_id, "incidentio")
        if not creds or not creds.get("api_key"):
            return {}

        client = IncidentioClient(creds["api_key"])
        data = client.list_alert_priorities()
        # Alert Priority catalog rank: higher = more urgent (matches our
        # "higher stored value = more severe" convention, so no inversion).
        for prio in data.get("severities", []) or []:
            name = prio.get("name")
            rank = prio.get("rank")
            if isinstance(name, str) and isinstance(rank, int):
                ranks[name.lower().strip()] = rank
    except IncidentioAPIError as exc:
        # 401/403 mean the key can't read alert priorities — a persistent,
        # actionable config problem. Mark it denied so we stop retrying every
        # alert. All other API errors are transient and must NOT be cached.
        if exc.code in (IncidentioAPIError.FORBIDDEN, IncidentioAPIError.INVALID_KEY):
            denied = True
            logger.warning(
                "[INCIDENTIO] API key lacks permission to read alert priorities (View data scope) "
                "for user %s — alert priorities cannot be ranked and will fail open. "
                "Grant the key the 'View data' permission to enable priority filtering.",
                user_id,
            )
        else:
            logger.warning(
                "[INCIDENTIO] Transient error fetching org alert priorities for user %s (%s)",
                user_id, exc.code,
            )
            return {}
    except Exception:
        # Unexpected failure — fail open and retry next time (don't cache).
        logger.warning("[INCIDENTIO] Could not fetch org alert priorities for user %s", user_id)
        return {}

    # Persist the result: a denied marker for permission failures, otherwise
    # the fetched catalog. Both are re-checked when their TTL expires.
    try:
        from utils.cache.redis_client import get_redis_client
        rc = get_redis_client()
        if rc is not None:
            if denied:
                rc.set(
                    cache_key,
                    _ORG_SEVERITY_DENIED_MARKER,
                    ex=_ORG_SEVERITY_DENIED_TTL_SECONDS,
                )
            else:
                rc.set(cache_key, _json.dumps(ranks), ex=_ORG_SEVERITY_CACHE_TTL_SECONDS)
    except Exception:
        logger.debug("[INCIDENTIO] Severity-rank cache write failed", exc_info=True)

    return ranks


def invalidate_org_severity_cache(user_id: str) -> None:
    """Drop the cached severity catalog (and any denied marker) for a user.

    Called on connect/disconnect so a new or rotated API key re-fetches the
    catalog instead of serving a stale list or a stale "denied" marker.
    """
    try:
        from utils.cache.redis_client import get_redis_client
        rc = get_redis_client()
        if rc is not None:
            rc.delete(f"incidentio:severity_ranks:{user_id}")
    except Exception:
        logger.debug("[INCIDENTIO] Severity cache invalidation failed", exc_info=True)


def get_org_severities(user_id: str) -> Dict[str, Any]:
    """Return the org's alert priorities for display, plus availability metadata.

    Shape: {"available": bool, "denied": bool,
            "severities": [{"name": str, "rank": int}, ...]} sorted most-urgent
    first. ``rank`` is the org's Alert Priority catalog rank (higher = more
    urgent). ``available`` is False when the catalog couldn't be read (e.g. the
    API key lacks the "View data" scope), so the UI can disable selection and
    explain why.
    """
    is_denied = False

    # Peek at the cache first so a known "denied" key reports unavailable
    # without another API round-trip.
    try:
        from utils.cache.redis_client import get_redis_client
        rc = get_redis_client()
        if rc is not None and rc.get(f"incidentio:severity_ranks:{user_id}") == _ORG_SEVERITY_DENIED_MARKER:
            is_denied = True
    except Exception:
        logger.debug("[INCIDENTIO] Severity availability cache read failed", exc_info=True)

    ranks = _get_org_severity_ranks(user_id)

    # Re-check the denied marker: _get_org_severity_ranks may have just set it.
    if not ranks and not is_denied:
        try:
            from utils.cache.redis_client import get_redis_client
            rc = get_redis_client()
            if rc is not None and rc.get(f"incidentio:severity_ranks:{user_id}") == _ORG_SEVERITY_DENIED_MARKER:
                is_denied = True
        except Exception:
            logger.debug("[INCIDENTIO] Severity availability cache re-check failed", exc_info=True)

    # Available only when we actually have priorities to show. An empty-but-
    # not-denied result (transient error) is also reported unavailable so the
    # UI doesn't render an empty dropdown. Sort by rank descending = most-urgent
    # first (higher rank = more urgent).
    severities = [
        {"name": name, "rank": rank}
        for name, rank in sorted(ranks.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {"available": bool(severities), "denied": is_denied, "severities": severities}


def _should_trigger_rca(user_id: str) -> bool:
    """Master switch for RCA on any incident.io event (default on)."""
    from utils.auth.stateless_auth import get_user_preference
    return get_user_preference(user_id, "incidentio_rca_enabled", default=True)


def _should_trigger_alert_rca(user_id: str) -> bool:
    """Whether RCA should run for incident.io *alert* events (public_alert.*).

    Some orgs fire alerts constantly and only want RCA on declared incidents,
    so this is a separate opt-in (default True) gated behind the master
    ``incidentio_rca_enabled`` switch.
    """
    from utils.auth.stateless_auth import get_user_preference
    return get_user_preference(user_id, "incidentio_alert_rca_enabled", default=True)


def _normalize_via_org_rank(raw_severity: str, org_ranks: Dict[str, int]) -> Optional[str]:
    """Map a custom severity name to a normalized bucket using the org's ranks.

    incident.io orgs define arbitrary severity names, but each has a numeric
    rank (lower = less severe). We bucket the alert's severity into our fixed
    low/medium/high/critical scale by its *relative position* within the org's
    own catalog, so a custom "Degraded" between "Minor" and "Major" lands in a
    sensible bucket instead of "unknown".

    Returns None when the name isn't in the catalog or the catalog is unusable,
    signalling the caller to fall back to name-based mapping.
    """
    if not org_ranks:
        return None

    key = (raw_severity or "").lower().strip()
    if key not in org_ranks:
        return None

    ranks = sorted(set(org_ranks.values()))
    # A single defined severity can't establish a gradient — treat as high so
    # it isn't accidentally filtered out as low-severity noise.
    if len(ranks) < 2:
        return "high"

    lo, hi = ranks[0], ranks[-1]
    rank = org_ranks[key]
    # Position of this severity within the org's range, scaled to [0, 1].
    fraction = (rank - lo) / (hi - lo)

    # Split the normalized scale into quartiles by relative position.
    if fraction >= 0.75:
        return "critical"
    if fraction >= 0.5:
        return "high"
    if fraction >= 0.25:
        return "medium"
    return "low"


def _severity_passes_filter(
    user_id: str, normalized_severity: str, raw_severity: str = ""
) -> bool:
    """Decide whether an alert's severity clears the org's RCA filter.

    Two customizable knobs (per-user preferences):
    - ``incidentio_alert_min_severity``: minimum severity to investigate
      (e.g. "high" skips low/medium noise). Default "low".
    - ``incidentio_alert_severity_allowlist``: optional explicit list of
      severities to investigate. When set (non-empty), it takes precedence
      over the minimum-severity threshold.

    Because incident.io severities are org-customizable, we first try to
    resolve the alert's *raw* severity name against the org's severity catalog
    (by numeric rank). Only when that isn't possible do we fall back to the
    fixed name-based mapping.

    This lets an org investigate specific critical alerts while ignoring
    high-volume low-severity noise.
    """
    from utils.auth.stateless_auth import get_user_preference

    sev = (normalized_severity or "unknown").lower()
    raw = (raw_severity or "").lower().strip()

    # Fetch the org catalog once; used both to rank the incoming alert and to
    # resolve a real-severity-name threshold below.
    org_ranks = _get_org_severity_ranks(user_id)

    # A custom severity name maps to "unknown" under the fixed buckets — try
    # to recover a real bucket from the org's own severity ranks so the filter
    # actually applies to custom severities instead of always passing them.
    if sev == "unknown" and raw:
        recovered = _normalize_via_org_rank(raw, org_ranks)
        if recovered is not None:
            sev = recovered

    # Explicit allowlist wins when configured — most precise control. Match
    # against both the normalized bucket and the raw org-specific name so orgs
    # can allowlist custom severity labels directly.
    allowlist = get_user_preference(
        user_id, "incidentio_alert_severity_allowlist", default=None
    )
    if isinstance(allowlist, list) and allowlist:
        allowed = {str(s).lower() for s in allowlist}
        return sev in allowed or raw in allowed

    # Otherwise fall back to a minimum-severity threshold.
    min_sev = str(
        get_user_preference(user_id, "incidentio_alert_min_severity", default="low")
    ).lower()

    # Threshold is a real org severity name (not one of our fixed buckets):
    # compare by the org's own rank when we can resolve the alert's rank too.
    # This is the precise path when the user picked from their real severities.
    if min_sev in org_ranks:
        threshold_rank = org_ranks[min_sev]
        if raw in org_ranks:
            return org_ranks[raw] >= threshold_rank
        # Alert severity isn't in the catalog — ambiguous, so fail open rather
        # than drop it against a real-name threshold.
        return True

    # Both threshold and alert severity are recognized fixed buckets: compare
    # them on the fixed low<medium<high<critical scale. This must run before the
    # catalog fallback so that (e.g.) a "low"/"medium" alert is correctly
    # dropped against a "high" threshold even when the org catalog is empty or
    # custom (previously these leaked through the unconditional fail-open below).
    if min_sev in _SEVERITY_ORDER and sev in _SEVERITY_ORDER:
        return _SEVERITY_ORDER[sev] >= _SEVERITY_ORDER[min_sev]

    # Threshold couldn't be resolved against the org's real priority ranks
    # (custom severity not in catalog, none provided, or catalog unavailable)
    # and it isn't a recognized fixed bucket either — ambiguous, so never
    # filter it out: better to over-investigate than to silently drop an alert
    # we couldn't classify.
    return True


def _should_postback(user_id: str) -> bool:
    from utils.auth.stateless_auth import get_user_preference
    return get_user_preference(user_id, "incidentio_postback_enabled", default=False)


def _resolve_incident_object(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Find the incident dict inside an incident.io webhook payload.

    incident.io sends two families of events:
    - Incident events (public_incident.*/private_incident.*): nested under
      event.incident, payload.incident, or keyed by the event-type topic name
    - Alert events (public_alert.*/private_alert.*): alert data is a direct
      child of the payload, either under event.alert/payload.alert or keyed
      by the event-type topic name
    """
    event = payload.get("event", {}) or {}
    incident = event.get("incident") or payload.get("incident") or None

    # Public and private incidents can arrive keyed by their topic name
    # (e.g. "private_incident.incident_created_v2": {"incident": {...}}) — treat
    # both families identically so private declared incidents aren't dropped.
    if not incident:
        for key, value in payload.items():
            if ("public_incident." in key or "private_incident." in key) and isinstance(value, dict):
                incident = value.get("incident") or value
                break

    if not incident:
        alert = event.get("alert") or payload.get("alert")
        if isinstance(alert, dict):
            return alert

    # Private and public alerts can arrive keyed by their topic name
    # (e.g. "private_alert.alert_created_v1": {...}) rather than under
    # event.alert — treat both alert families identically.
    if not incident:
        for key, value in payload.items():
            if ("public_alert." in key or "private_alert." in key) and isinstance(value, dict):
                alert = value.get("alert") or value
                if isinstance(alert, dict):
                    return alert

    return incident or {}


def _safe_name(obj, default: str = "") -> str:
    """Extract .name from a dict-or-scalar field."""
    if isinstance(obj, dict):
        return obj.get("name", default)
    return str(obj) if obj else default


def _priority_from_attributes(incident: Dict[str, Any]) -> Optional[str]:
    """Read the resolved priority from the structured AlertPriority attribute.

    incident.io surfaces the resolved priority as an attribute
    (attribute.type == "AlertPriority") with the human name in value.label —
    e.g. "Urgent". Returns None when no such attribute carries a usable label.
    """
    attributes = incident.get("attributes")
    if not isinstance(attributes, list):
        return None

    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        meta = attr.get("attribute") or {}
        if meta.get("type") != "AlertPriority":
            continue
        value = attr.get("value") or {}
        label = value.get("label")
        if isinstance(label, str) and label.strip():
            return label
    return None


def _priority_from_metadata(incident: Dict[str, Any]) -> Optional[str]:
    """Fall back to raw metadata (severity/priority/level) for alert sources
    that send priority inline rather than as a structured attribute."""
    raw_metadata = incident.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    for key in ("severity", "priority", "level"):
        if key in metadata:
            return str(metadata[key])
    return None


def _extract_alert_priority(incident: Dict[str, Any]) -> str:
    """Pull the alert's priority name from an incident.io alert object.

    Prefers the structured AlertPriority attribute (whose label matches the
    org's Alert Priority catalog names our RCA filter ranks against), then
    falls back to raw metadata, then to "unknown".
    """
    # Preferred: the structured AlertPriority attribute (value.label).
    label = _priority_from_attributes(incident)
    if label is not None:
        return label

    # Fallback: some alert sources put priority/severity directly in metadata.
    from_metadata = _priority_from_metadata(incident)
    if from_metadata is not None:
        return from_metadata

    return "unknown"


def _attr_by_keywords(attrs: Dict[str, str], keywords: tuple) -> str:
    """Return the first attribute whose name contains any of ``keywords``.

    Attribute names are org-specific (e.g. "Labels.service", "Affected service"),
    so we match on substrings rather than exact names. Returns "" when none
    match.
    """
    for name, value in attrs.items():
        if any(kw in name for kw in keywords):
            return value
    return ""


def _service_from_alert_attributes(attrs: Dict[str, str]) -> str:
    """Best-effort service name from an alert's structured attributes."""
    return _attr_by_keywords(attrs, ("service", "component", "application", "app"))


def _extract_incident_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalized incident fields from the webhook event envelope.

    Handles both incident events (public_incident.*/private_incident.*) and
    alert events (public_alert.*/private_alert.*). Alert events carry
    title/description/status/metadata directly on the alert object rather
    than in incident-shaped fields.
    """
    event = payload.get("event", {}) or {}
    incident = _resolve_incident_object(payload)

    event_type = payload.get("event_type") or event.get("type", "")
    is_alert_event = "alert" in event_type.lower()

    if is_alert_event and not incident.get("name"):
        # Priority comes from the AlertPriority attribute (or metadata fallback).
        severity_raw = _extract_alert_priority(incident)
        raw_metadata = incident.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

        # Read all structured alert attributes generically (service/environment/
        # team/etc.) so the RCA sees real context instead of "unknown". Attribute
        # names are org-specific, so match on common name substrings rather than
        # one org's exact labels.
        alert_attributes = _extract_alert_attributes(incident)
        service = _service_from_alert_attributes(alert_attributes)
        environment = _attr_by_keywords(alert_attributes, ("environment", "env", "stage"))

        # Alerts have no incident_id, but they carry a stable identifier we can
        # use for storage/dedup: the alert's own id or the source dedup key.
        # Without this the ON CONFLICT (org_id, incident_id) upsert can never
        # dedup (NULLs are distinct in Postgres) and Svix retries would pile up
        # duplicate rows.
        alert_ref = (
            incident.get("id")
            or incident.get("deduplication_key")
            or payload.get("id")
        )

        return {
            "incident_id": alert_ref,
            "incident_name": incident.get("title") or "Untitled Alert",
            "incident_status": incident.get("status") or "firing",
            "severity": severity_raw,
            # Prefer a real service attribute; fall back to metadata source/service.
            "incident_type": service or metadata.get("source") or metadata.get("service") or "",
            "summary": incident.get("description") or "",
            "created_at": incident.get("created_at"),
            "updated_at": incident.get("updated_at"),
            "permalink": incident.get("source_url") or "",
            "custom_fields": [],
            "roles": [],
            "is_alert": True,
            # Alert-specific context surfaced to the RCA (empty for incidents).
            "priority": severity_raw if severity_raw != "unknown" else "",
            "environment": environment,
            "alert_attributes": alert_attributes,
        }

    return {
        "incident_id": incident.get("id") or payload.get("id"),
        "incident_name": incident.get("name") or incident.get("title") or "Untitled Incident",
        "incident_status": incident.get("status") or event.get("status") or "unknown",
        "severity": _safe_name(incident.get("severity"), "unknown"),
        "incident_type": _safe_name(incident.get("incident_type")),
        "summary": incident.get("summary") or "",
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "permalink": incident.get("permalink") or "",
        "custom_fields": incident.get("custom_field_entries") or [],
        "roles": incident.get("incident_role_assignments") or [],
        "is_alert": False,
    }


def _map_severity(severity_name: str) -> str:
    """Normalize incident.io severity names to standard levels."""
    s = severity_name.lower().strip()
    if s in ("critical", "sev0", "sev1", "p0", "p1"):
        return "critical"
    if s in ("high", "major", "sev2", "p2"):
        return "high"
    if s in ("medium", "moderate", "sev3", "p3"):
        return "medium"
    if s in ("low", "minor", "sev4", "sev5", "p4", "p5"):
        return "low"
    return "unknown"


def _normalize_alert_severity(user_id: str, raw_severity: str, *, is_alert: bool) -> str:
    """Normalize a stored/RCA severity from an incident.io severity/priority name.

    Incident events carry standard severity names (critical/high/...) that
    ``_map_severity`` handles directly. Alert events instead carry the org's
    Alert Priority name (e.g. "Urgent", "In-hours"), which isn't in the fixed
    scale — so for alerts we first try to rank the priority against the org's
    catalog (top→critical, bottom→low) and only fall back to name-based mapping
    when the catalog can't resolve it. This keeps a real priority from being
    flattened to "unknown" in both the stored row and the RCA summary.
    """
    # Fixed-name mapping is correct for incident events and for alert priorities
    # that happen to use standard names (e.g. "Critical").
    mapped = _map_severity(raw_severity)
    if not is_alert or mapped != "unknown":
        return mapped

    # Alert priority isn't a fixed bucket — rank it against the org catalog.
    recovered = _normalize_via_org_rank(raw_severity, _get_org_severity_ranks(user_id))
    return recovered if recovered is not None else "unknown"


_NEW_INCIDENT_EVENTS = frozenset((
    "incident.created", "v2.incidents.created",
    "incident.declared", "public_incident.incident_created",
    "public_incident.incident_created_v2",
    # Private incidents fire the same lifecycle as public ones on a separate
    # topic — process them identically so private declared incidents aren't
    # silently dropped at the trigger gate.
    "private_incident.incident_created",
    "private_incident.incident_created_v2",
    "public_alert.alert_created_v1",
    # Private alerts fire the same lifecycle as public ones on a separate
    # topic — process them identically so private-alert RCA isn't silently
    # dropped at the trigger gate.
    "private_alert.alert_created_v1",
))


def _extract_alert_attributes(incident: Dict[str, Any]) -> Dict[str, str]:
    """Read an alert's structured attributes into a flat {name: label} map.

    incident.io alerts carry a list of ``attributes``, each shaped like
    ``{"attribute": {"name": "...", "type": "..."}, "value": {"label": "..."}}``
    (values can also be a list for multi-select attributes). We read them
    generically by attribute name — rather than hard-coding one org's labels —
    so downstream code can pull service/environment/team from whatever the org
    actually configured (e.g. "Labels.service", "Team", "Environment").

    Keys are lowercased attribute names; values are the human-readable labels.
    """
    result: Dict[str, str] = {}
    attributes = incident.get("attributes")
    if not isinstance(attributes, list):
        return result

    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        meta = attr.get("attribute") or {}
        name = meta.get("name")
        if not isinstance(name, str) or not name.strip():
            continue

        # Value can be a single object or a list (multi-select) — collect labels.
        raw_value = attr.get("value")
        labels: list = []
        if isinstance(raw_value, dict):
            labels = [raw_value.get("label") or raw_value.get("literal")]
        elif isinstance(raw_value, list):
            labels = [
                (v.get("label") or v.get("literal"))
                for v in raw_value
                if isinstance(v, dict)
            ]

        clean = [str(v).strip() for v in labels if isinstance(v, str) and v.strip()]
        if clean:
            result[name.lower().strip()] = ", ".join(clean)

    return result


def _build_alert_metadata(fields: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "permalink": fields["permalink"],
        "summary": fields["summary"],
        "event_type": event_type,
    }
    # Surface the alert's own priority/service/environment (and any other
    # structured attributes) so the RCA prompt sees them instead of only the
    # permalink/summary. Empty for incident events (no alert attributes).
    if fields.get("priority"):
        meta["priority"] = fields["priority"]
    if fields.get("environment"):
        meta["environment"] = fields["environment"]
    alert_attrs = fields.get("alert_attributes")
    if isinstance(alert_attrs, dict) and alert_attrs:
        meta["alert_attributes"] = alert_attrs
    if fields["roles"]:
        meta["roles"] = [
            {"role": r.get("role", {}).get("name", ""),
             "assignee": r.get("assignee", {}).get("name", "")}
            for r in fields["roles"][:5]
        ]
    return meta


def _try_correlate(cursor, conn, *, user_id, alert_db_id, fields, service,
                   normalized_severity, alert_metadata, payload, org_id) -> bool:
    """Attempt alert correlation. Returns True if correlated (and committed)."""
    try:
        cursor.execute("SAVEPOINT sp_correlation")
        correlator = AlertCorrelator()
        result = correlator.correlate(
            cursor=cursor, user_id=user_id, source_type="incidentio",
            source_alert_id=alert_db_id, alert_title=fields["incident_name"],
            alert_service=service, alert_severity=normalized_severity,
            alert_metadata=alert_metadata, org_id=org_id,
        )
        if result.is_correlated and apply_correlation_outcome(
            cursor=cursor, user_id=user_id, incident_id=result.incident_id,
            source_type="incidentio", source_alert_id=alert_db_id,
            alert_title=fields["incident_name"], alert_service=service,
            alert_severity=normalized_severity, correlation_result=result,
            alert_metadata=alert_metadata, raw_payload=payload, org_id=org_id,
            # Live hint-only mode needs the fall-through to actually create
            # an incident; with RCA disabled it would not, so keep the
            # legacy attach in that case.
            hint_only_eligible=_should_trigger_rca(user_id),
        ):
            conn.commit()
            return True
        cursor.execute("RELEASE SAVEPOINT sp_correlation")
    except Exception as corr_exc:
        cursor.execute("ROLLBACK TO SAVEPOINT sp_correlation")
        logger.warning("[INCIDENTIO] Correlation failed, continuing: %s", corr_exc)
    return False


def _create_and_link_incident(cursor, conn, *, user_id, org_id, alert_db_id,
                              fields, service, normalized_severity,
                              alert_metadata, received_at) -> Optional[str]:
    """Create Aurora incident record and link the alert. Returns incident_id or None."""
    # Environment (prod/staging/…) from the alert's attributes, when present.
    environment = fields.get("environment") or None
    cursor.execute(
        """
        INSERT INTO incidents
        (user_id, org_id, source_type, source_alert_id, alert_title,
         alert_service, alert_environment, severity, status, started_at, alert_metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
        SET updated_at = CURRENT_TIMESTAMP,
            alert_metadata = EXCLUDED.alert_metadata
        RETURNING id
        """,
        (
            user_id, org_id, "incidentio", alert_db_id,
            fields["incident_name"], service, environment, normalized_severity,
            "investigating", received_at, json.dumps(alert_metadata),
        ),
    )
    row = cursor.fetchone()
    incident_id = row[0] if row else None
    conn.commit()

    if not incident_id:
        return None

    try:
        cursor.execute(
            """INSERT INTO incident_alerts
               (user_id, org_id, incident_id, source_type, source_alert_id,
                alert_title, alert_service, alert_severity, correlation_strategy,
                correlation_score, alert_metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id, org_id, incident_id, "incidentio", alert_db_id,
                fields["incident_name"], service, normalized_severity,
                "primary", 1.0, json.dumps(alert_metadata),
            ),
        )
        cursor.execute(
            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
            (service, incident_id),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning("[INCIDENTIO] Failed to link alert: %s", e)

    return str(incident_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="incidentio.process_event"
)
def process_incidentio_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process an incident.io webhook event."""
    try:
        event_type = payload.get("event_type") or (payload.get("event", {}) or {}).get("type", "unknown")
        fields = _extract_incident_fields(payload)
        logger.info(
            "[INCIDENTIO][EVENT][USER:%s] type=%s incident=%s status=%s severity=%s",
            user_id or "unknown", event_type, fields["incident_name"],
            fields["incident_status"], fields["severity"],
        )

        if not user_id:
            logger.warning("[INCIDENTIO] No user_id — event not stored")
            return

        _store_and_process_event(user_id, event_type, fields, payload)

    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to process event")
        raise self.retry(exc=exc)


def _store_and_process_event(user_id: str, event_type: str,
                             fields: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Store the alert and optionally trigger correlation/RCA."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    incident_id = None
    service = ""
    normalized_severity = ""
    alert_metadata: Dict[str, Any] = {}

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[INCIDENTIO]")
            if not org_id:
                return

            received_at = datetime.now(timezone.utc)
            is_alert = bool(fields.get("is_alert"))
            # For alerts, the "severity" is really the org's Alert Priority name
            # (e.g. "Urgent"), which _map_severity can't understand and would
            # flatten to "unknown". Rank it against the org's catalog so a real
            # priority maps to a real bucket (top→critical, bottom→low) and the
            # stored severity / RCA summary reflect the true priority.
            normalized_severity = _normalize_alert_severity(
                user_id, fields["severity"], is_alert=is_alert
            )

            # Alert events (public_alert.*) carry no incident_id; we synthesize a
            # stable ref (alert id / dedup key) in _extract_incident_fields. Only
            # drop if even that is missing, since without any key we can't dedup
            # or link the event.
            if not fields.get("incident_id"):
                logger.error(
                    "[INCIDENTIO] Event has no extractable identifier, dropping event for user %s",
                    user_id,
                )
                return

            alert_db_id = _upsert_alert(cursor, conn, user_id=user_id, org_id=org_id,
                                        fields=fields, payload=payload,
                                        severity=normalized_severity, received_at=received_at)
            if not alert_db_id:
                conn.rollback()
                logger.error("[INCIDENTIO] Failed to store event for user %s", user_id)
                return

            if event_type not in _NEW_INCIDENT_EVENTS:
                conn.commit()
                logger.info("[INCIDENTIO] Stored update event (no RCA trigger)")
                return

            service = _extract_service(fields)
            alert_metadata = _build_alert_metadata(fields, event_type)

            # Correlation runs regardless of the RCA gate: attaching a new
            # event to an existing open incident is useful even when we won't
            # kick off a fresh investigation (legacy hint-only mode).
            if _try_correlate(cursor, conn, user_id=user_id, alert_db_id=alert_db_id,
                              fields=fields, service=service,
                              normalized_severity=normalized_severity,
                              alert_metadata=alert_metadata, payload=payload, org_id=org_id):
                return

            # Master RCA switch — off means store only, never investigate.
            if not _should_trigger_rca(user_id):
                conn.commit()
                logger.info("[INCIDENTIO] Stored event (RCA disabled)")
                return

            # Alert-specific gating: orgs can disable alert RCA entirely, or
            # filter by severity to avoid investigating high-volume low-severity
            # noise while still catching critical alerts. Incident events are
            # unaffected by these knobs.
            if is_alert:
                if not _should_trigger_alert_rca(user_id):
                    conn.commit()
                    logger.info("[INCIDENTIO] Stored alert (alert RCA disabled for user %s)", user_id)
                    return
                if not _severity_passes_filter(
                    user_id, normalized_severity, raw_severity=fields.get("severity", "")
                ):
                    conn.commit()
                    logger.info(
                        "[INCIDENTIO] Stored alert (severity=%s filtered out for user %s)",
                        normalized_severity, user_id,
                    )
                    return

            incident_id = _create_and_link_incident(
                cursor, conn, user_id=user_id, org_id=org_id,
                alert_db_id=alert_db_id, fields=fields, service=service,
                normalized_severity=normalized_severity,
                alert_metadata=alert_metadata, received_at=received_at,
            )

    if incident_id:
        _trigger_rca_pipeline(
            user_id=user_id, incident_id=incident_id, fields=fields,
            payload=payload, alert_metadata=alert_metadata,
            service=service, severity=normalized_severity,
        )


def _upsert_alert(cursor, conn, *, user_id, org_id, fields, payload,
                   severity, received_at) -> Optional[int]:
    """Insert or update the incidentio_alerts row. Returns the DB id or None."""
    cursor.execute(
        """
        INSERT INTO incidentio_alerts
        (user_id, org_id, incident_id, incident_name, incident_status,
         severity, incident_type, payload, received_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (org_id, incident_id) DO UPDATE
        SET incident_name = EXCLUDED.incident_name,
            incident_status = EXCLUDED.incident_status,
            severity = EXCLUDED.severity,
            incident_type = EXCLUDED.incident_type,
            payload = EXCLUDED.payload,
            received_at = EXCLUDED.received_at
        RETURNING id
        """,
        (
            user_id, org_id, fields["incident_id"],
            fields["incident_name"], fields["incident_status"],
            severity, fields["incident_type"],
            json.dumps(payload), received_at,
        ),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _extract_service(fields: Dict[str, Any]) -> str:
    """Best-effort service extraction from incident fields."""
    for cf in fields.get("custom_fields") or []:
        field_def = cf.get("custom_field", {})
        if field_def.get("name", "").lower() in ("service", "affected_service", "component"):
            values = cf.get("values") or []
            if values:
                return str(values[0].get("label") or values[0].get("value", ""))[:255]

    name = fields.get("incident_name", "")
    if ":" in name:
        return name.split(":")[0].strip()[:255]

    return fields.get("incident_type") or "unknown"


def _mark_rca_skipped(incident_id: str, reason: str) -> None:
    """Record on the incident that RCA was intentionally skipped (with reason).

    Surfaces skips (e.g. rate limiting) on the dashboard instead of leaving the
    operator with only a log line and an incident stuck at 'idle'.
    """
    from utils.db.connection_pool import db_pool
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE incidents SET aurora_status = %s, aurora_summary = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    ("skipped", reason, str(incident_id)),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("[INCIDENTIO] Failed to mark RCA skipped for incident %s: %s", incident_id, exc)


def _trigger_rca_pipeline(
    user_id: str,
    incident_id: str,
    fields: Dict[str, Any],
    payload: Dict[str, Any],
    alert_metadata: Dict[str, Any],
    service: str,
    severity: str,
) -> None:
    """Trigger summary generation and background RCA for an incident."""
    from chat.background.summarization import generate_incident_summary

    generate_incident_summary.delay(
        incident_id=str(incident_id),
        user_id=user_id,
        source_type="incidentio",
        alert_title=fields["incident_name"],
        severity=severity,
        service=service,
        raw_payload=payload,
        alert_metadata=alert_metadata,
    )

    try:
        from chat.background.task import (
            run_background_chat,
            create_background_chat_session,
            is_background_chat_allowed,
        )

        if not is_background_chat_allowed(user_id):
            # Rate limited: don't silently drop the alert. Record the reason on
            # the incident so it's visible on the dashboard instead of just an
            # INFO log the operator never sees. RCA can be re-run manually.
            logger.info("[INCIDENTIO] Background RCA rate-limited for user %s", user_id)
            _mark_rca_skipped(
                incident_id,
                "RCA skipped: background-investigation rate limit reached. "
                "Re-run the investigation manually from this incident.",
            )
            return

        chat_title = f"RCA: {fields['incident_name']}"
        session_id = create_background_chat_session(
            user_id=user_id,
            title=chat_title,
            trigger_metadata={
                "source": "incidentio",
                "incident_id": fields["incident_id"],
                "incident_name": fields["incident_name"],
                "permalink": fields["permalink"],
            },
            incident_id=str(incident_id),
        )

        rca_prompt, rail_text = build_rca_prompt("incidentio", fields["incident_name"], payload, user_id=user_id)

        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=rca_prompt,
            trigger_metadata={
                "source": "incidentio",
                "incident_id": fields["incident_id"],
                "incident_name": fields["incident_name"],
            },
            incident_id=str(incident_id),
            rail_text=rail_text,
        )

        # Store task ID for cancellation support
        from utils.db.connection_pool import db_pool
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                        (task.id, str(incident_id)),
                    )
                    conn.commit()
        except Exception as exc:
            logger.warning("[INCIDENTIO] Failed to store RCA task ID for incident %s: %s", incident_id, exc)

        logger.info("[INCIDENTIO] Triggered RCA for incident %s (task=%s)", incident_id, task.id)

        # Post-back RCA summary if enabled. Only incident events have an
        # incident.io timeline to post to — alert events (public_alert.*) have
        # no /incident_updates endpoint, so skip postback for them.
        if _should_postback(user_id) and fields.get("incident_id") and not fields.get("is_alert"):
            postback_rca_to_incidentio.delay(user_id, str(incident_id), fields["incident_id"])

    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to trigger RCA: %s", exc)


@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=120, name="incidentio.postback_rca"
)
def postback_rca_to_incidentio(
    self,
    user_id: str,
    aurora_incident_id: str,
    incidentio_incident_id: str,
) -> None:
    """Post RCA results back to incident.io timeline once analysis completes."""
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.token_management import get_token_data
        from routes.incidentio.incidentio_routes import IncidentioClient

        # Wait for RCA to complete — check for summary in incidents table
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT aurora_summary, aurora_status FROM incidents WHERE id = %s",
                    (aurora_incident_id,),
                )
                row = cursor.fetchone()

        if not row or not row[0]:
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=120)
            logger.info("[INCIDENTIO] No RCA summary after retries, skipping postback")
            return

        summary, aurora_status = row
        if aurora_status not in ("analyzed", "completed"):
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=120)
            return

        creds = get_token_data(user_id, "incidentio")
        if not creds or not creds.get("api_key"):
            logger.warning("[INCIDENTIO] No credentials for postback")
            return

        client = IncidentioClient(creds["api_key"])
        message = f"🔍 **Aurora RCA Summary**\n\n{summary}"
        client.post_incident_update(incidentio_incident_id, message)
        logger.info("[INCIDENTIO] Posted RCA back to incident %s", incidentio_incident_id)

    except Exception as exc:
        if "retry" not in str(type(exc).__name__).lower():
            logger.exception("[INCIDENTIO] Postback failed: %s", exc)
            raise self.retry(exc=exc)
        raise

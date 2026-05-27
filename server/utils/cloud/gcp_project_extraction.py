"""Pure helper to extract a GCP project ID from heterogeneous alert payloads.

No DB or network access. Safe to call from any context. Returns ``None`` when
no valid project ID can be extracted.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# GCP project IDs: 6-30 chars, start with lowercase letter, lowercase letters /
# digits / hyphens only.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{5,29}$")
_PROJECTS_PATH_RE = re.compile(r"projects/([a-z][a-z0-9\-]{5,29})(?:/|$)")

# Keys we consider authoritative when they appear in a key/value mapping.
_PROJECT_KEYS = ("project_id", "gcp_project", "gcp_project_id", "projectId", "project")


def _valid(pid: Any) -> Optional[str]:
    if not isinstance(pid, str):
        return None
    pid = pid.strip()
    if _PROJECT_ID_RE.match(pid):
        return pid
    return None


def _from_tag_list(tags: Iterable[Any]) -> Optional[str]:
    """Datadog-style: ['key:value', 'project_id:foo']."""
    try:
        for tag in tags:
            if not isinstance(tag, str) or ":" not in tag:
                continue
            key, _, value = tag.partition(":")
            if key.strip() in _PROJECT_KEYS:
                pid = _valid(value)
                if pid:
                    return pid
    except Exception:
        return None
    return None


def _from_mapping(mapping: Any) -> Optional[str]:
    try:
        if not isinstance(mapping, dict):
            return None
        for key in _PROJECT_KEYS:
            if key in mapping:
                pid = _valid(mapping[key])
                if pid:
                    return pid
    except Exception:
        return None
    return None


def _from_sentry_tag_pairs(pairs: Any) -> Optional[str]:
    """Sentry-style: [['key', 'value'], ['project_id', 'foo']]."""
    try:
        if not isinstance(pairs, list):
            return None
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            key, value = pair[0], pair[1]
            if isinstance(key, str) and key in _PROJECT_KEYS:
                pid = _valid(value)
                if pid:
                    return pid
    except Exception:
        return None
    return None


def _scan_strings(node: Any, depth: int = 0) -> Optional[str]:
    """Walk arbitrary structure looking for ``projects/<id>/`` strings."""
    if depth > 6:
        return None
    try:
        if isinstance(node, str):
            m = _PROJECTS_PATH_RE.search(node)
            if m:
                pid = _valid(m.group(1))
                if pid:
                    return pid
            return None
        if isinstance(node, dict):
            for v in node.values():
                found = _scan_strings(v, depth + 1)
                if found:
                    return found
            return None
        if isinstance(node, (list, tuple)):
            for v in node:
                found = _scan_strings(v, depth + 1)
                if found:
                    return found
            return None
    except Exception:
        return None
    return None


def extract_gcp_project_from_alert(payload: dict, source: str) -> Optional[str]:
    """Pull a GCP project ID out of an alert payload.

    Handles:
      - Datadog:   payload['tags'] = ["project_id:foo", "gcp_project:foo", ...]
      - Grafana:   payload['commonLabels']/['labels'] = {"gcp_project": ...} or
                   {"project_id": ...}
      - Sentry:    payload['data']['issue']['tags'] (list of [k,v]) /
                   contexts.gcp.project_id
      - PagerDuty: payload['custom_details'] dict + generic scan
      - Generic fallback: scan all string values for ``projects/<id>/`` patterns.

    Returns None when no valid project ID can be extracted.
    """
    if not isinstance(payload, dict):
        return None

    try:
        src = (source or "").lower()

        # --- Datadog ----------------------------------------------------------
        if src == "datadog":
            pid = _from_tag_list(payload.get("tags") or [])
            if pid:
                return pid

        # --- Grafana ----------------------------------------------------------
        if src == "grafana":
            for key in ("commonLabels", "labels", "groupLabels"):
                pid = _from_mapping(payload.get(key))
                if pid:
                    return pid
            # alerts: [{ labels: {...} }, ...]
            alerts = payload.get("alerts")
            if isinstance(alerts, list):
                for alert in alerts:
                    if isinstance(alert, dict):
                        pid = _from_mapping(alert.get("labels"))
                        if pid:
                            return pid

        # --- Sentry -----------------------------------------------------------
        if src == "sentry":
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            issue = data.get("issue") if isinstance(data.get("issue"), dict) else {}
            event = data.get("event") if isinstance(data.get("event"), dict) else {}
            for tag_holder in (issue, event, payload):
                if not isinstance(tag_holder, dict):
                    continue
                pid = _from_sentry_tag_pairs(tag_holder.get("tags"))
                if pid:
                    return pid
                contexts = tag_holder.get("contexts")
                if isinstance(contexts, dict):
                    gcp_ctx = contexts.get("gcp")
                    pid = _from_mapping(gcp_ctx)
                    if pid:
                        return pid

        # --- PagerDuty --------------------------------------------------------
        if src == "pagerduty":
            for key in ("custom_details", "details", "payload"):
                pid = _from_mapping(payload.get(key))
                if pid:
                    return pid

        # --- Generic top-level mapping check ---------------------------------
        pid = _from_mapping(payload)
        if pid:
            return pid

        # --- Generic fallback: scan for ``projects/<id>/`` patterns ----------
        return _scan_strings(payload)

    except Exception as e:
        logger.debug(
            "extract_gcp_project_from_alert failed for source=%s: %s",
            source,
            type(e).__name__,
        )
        return None

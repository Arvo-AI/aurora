"""Runbook utilities for PagerDuty integration.

Provides reusable functions for extracting, fetching, and validating
runbook content from PagerDuty alerts.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from connectors.confluence_connector.client import parse_confluence_page_id
from connectors.confluence_connector.runbook_utils import fetch_confluence_runbook

logger = logging.getLogger(__name__)


def extract_runbook_url(incident: Dict[str, Any]) -> Optional[str]:
    """Extract runbook URL from PagerDuty incident data.

    Checks both:
    1. Custom fields: incident['customFields']['runbook_link']
    2. Standard field: incident['runbook_url']

    Args:
        incident: PagerDuty incident data dictionary

    Returns:
        Runbook URL string if found, None otherwise
    """
    custom_fields = incident.get("customFields", {})
    if isinstance(custom_fields, dict):
        runbook_url = custom_fields.get("runbook_link")
        if runbook_url and isinstance(runbook_url, str):
            return runbook_url.strip()

    runbook_url = incident.get("runbook_url")
    if runbook_url and isinstance(runbook_url, str):
        return runbook_url.strip()

    return None


def fetch_runbook_content(
    url: str, user_id: Optional[str] = None, timeout: int = 10
) -> Optional[str]:
    """Fetch runbook content from a public URL or Confluence."""
    if _is_confluence_url(url):
        if not user_id:
            logger.warning(
                "[PAGERDUTY][RUNBOOK] Confluence URL provided without user_id: %s", url
            )
            return None
        result = fetch_confluence_runbook(url, user_id)
        return result.get("markdown") if result else None

    return _fetch_public_runbook_content(url, timeout=timeout)


def fetch_runbook_details(
    url: str, user_id: Optional[str] = None, timeout: int = 10
) -> Tuple[Optional[str], Optional[list]]:
    """Fetch runbook content and parsed steps if available."""
    if _is_confluence_url(url):
        if not user_id:
            logger.warning(
                "[PAGERDUTY][RUNBOOK] Confluence URL provided without user_id: %s", url
            )
            return None, None
        result = fetch_confluence_runbook(url, user_id)
        if not result:
            return None, None
        return result.get("markdown"), result.get("steps")

    return _fetch_public_runbook_content(url, timeout=timeout), None


def _fetch_public_runbook_content(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch runbook content from a public URL."""
    if not url.startswith(("http://", "https://")):
        logger.warning(
            "[PAGERDUTY][RUNBOOK] Invalid URL scheme (must be http/https): %s", url
        )
        return None

    try:
        response = requests.get(url, timeout=timeout, allow_redirects=True)

        if response.status_code != 200:
            logger.error(
                "[PAGERDUTY][RUNBOOK] Failed to fetch runbook - HTTP %d: %s",
                response.status_code,
                url,
            )
            return None

        content_type = response.headers.get("Content-Type", "").lower()
        if not any(ct in content_type for ct in ["text/", "application/text"]):
            logger.warning(
                "[PAGERDUTY][RUNBOOK] Invalid content type '%s' for runbook URL: %s",
                content_type,
                url,
            )
            return None

        content = response.text
        if not content or not content.strip():
            logger.warning("[PAGERDUTY][RUNBOOK] Runbook content is empty: %s", url)
            return None

        return content.strip()

    except Exception as e:
        logger.error(
            "[PAGERDUTY][RUNBOOK] Error fetching runbook from %s: %s",
            url,
            str(e),
            exc_info=True,
        )
        return None


def _is_confluence_url(url: str) -> bool:
    page_id = parse_confluence_page_id(url)
    if not page_id:
        return False
    hostname = urlparse(url).netloc.lower()
    return hostname.endswith(".atlassian.net") or "confluence" in hostname


def fetch_and_consolidate_pagerduty_events(
    user_id: str, incident_id: str, cursor
) -> Optional[Dict[str, Any]]:
    """Fetch all PagerDuty events for an incident and consolidate them."""
    cursor.execute(
        """
        SELECT event_type, payload FROM pagerduty_events 
        WHERE incident_id = %s
        AND (user_id = %s OR EXISTS (
            SELECT 1 FROM incidents 
            WHERE incidents.alert_metadata->>'incidentId' = pagerduty_events.incident_id
            AND (incidents.alert_metadata->>'is_demo')::boolean = true
        ))
        ORDER BY received_at ASC
        """,
        (incident_id, user_id),
    )
    events = cursor.fetchall()

    if not events:
        return None

    triggered_event = None
    custom_field_events = []

    for event_type, payload in events:
        if event_type == "incident.triggered":
            triggered_event = payload
        elif event_type == "incident.custom_field_values.updated":
            custom_field_events.append(payload)

    if not triggered_event:
        return events[-1][1] if events else None

    if not custom_field_events:
        return triggered_event

    try:
        merged = (
            json.loads(triggered_event)
            if isinstance(triggered_event, str)
            else triggered_event
        )

        all_custom_fields = {}
        for cf_event in custom_field_events:
            cf_payload = json.loads(cf_event) if isinstance(cf_event, str) else cf_event
            cf_data = cf_payload.get("event", {}).get("data", {})
            for field in cf_data.get("custom_fields", []):
                field_name = field.get("name")
                if field_name:
                    all_custom_fields[field_name] = field

        if all_custom_fields and "event" in merged:
            if "custom_fields" not in merged["event"]:
                merged["event"]["custom_fields"] = {}
            merged["event"]["custom_fields"].update(all_custom_fields)

            if "data" in merged["event"]:
                if "customFields" not in merged["event"]["data"]:
                    merged["event"]["data"]["customFields"] = {}
                for field_name, field_data in all_custom_fields.items():
                    merged["event"]["data"]["customFields"][field_name] = (
                        field_data.get("value")
                    )

        return merged

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(
            "[PAGERDUTY] Failed to merge custom fields for incident %s: %s",
            incident_id,
            str(e),
        )
        return triggered_event

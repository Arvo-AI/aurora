"""Runbook utilities for PagerDuty integration.

Provides reusable functions for extracting, fetching, and validating
runbook content from PagerDuty alerts.
"""

import json
import logging
import requests
from typing import Any, Dict, List, Optional, Tuple

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
    # Try custom fields first (PagerDuty V3 custom field pattern)
    custom_fields = incident.get("customFields", {})
    if isinstance(custom_fields, dict):
        runbook_url = custom_fields.get("runbook_link")
        if runbook_url and isinstance(runbook_url, str):
            return runbook_url.strip()
    
    # Try standard runbook_url field (Grafana-style)
    runbook_url = incident.get("runbook_url")
    if runbook_url and isinstance(runbook_url, str):
        return runbook_url.strip()
    
    return None


def fetch_runbook_content(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch runbook content from a public URL.
    
    Validates:
    - HTTP/HTTPS scheme only
    - Response status 200
    - Content-Type is text-based (text/plain, text/*, application/text)
    
    Args:
        url: Public URL to fetch runbook content from
        timeout: Request timeout in seconds (default: 10)
        
    Returns:
        Runbook text content if successful, None on any failure
    """
    # Validate URL scheme
    if not url.startswith(("http://", "https://")):
        logger.warning("[PAGERDUTY][RUNBOOK] Invalid URL scheme (must be http/https): %s", url)
        return None
    
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=True)
        
        # Check status code
        if response.status_code != 200:
            logger.error(
                "[PAGERDUTY][RUNBOOK] Failed to fetch runbook - HTTP %d: %s",
                response.status_code,
                url
            )
            return None
        
        # Validate content type
        content_type = response.headers.get("Content-Type", "").lower()
        if not any(ct in content_type for ct in ["text/", "application/text"]):
            logger.warning(
                "[PAGERDUTY][RUNBOOK] Invalid content type '%s' for runbook URL: %s",
                content_type,
                url
            )
            return None
        
        # Get text content
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
            exc_info=True
        )
        return None


def fetch_and_consolidate_pagerduty_events(
    user_id: str,
    incident_id: str,
    cursor
) -> Optional[Dict[str, Any]]:
    """Fetch all PagerDuty events for an incident and consolidate them.
    
    Fetches all events (triggered, acknowledged, resolved, custom field updates)
    and merges custom fields into the triggered event to provide a complete
    incident picture.
    
    Args:
        user_id: Aurora user ID
        incident_id: PagerDuty incident ID
        cursor: Database cursor
        
    Returns:
        Consolidated incident payload with merged custom fields, or None if no events found
    """
    # Fetch ALL events for this incident
    cursor.execute(
        """
        SELECT event_type, payload FROM pagerduty_events 
        WHERE user_id = %s AND incident_id = %s
        ORDER BY received_at ASC
        """,
        (user_id, incident_id)
    )
    events = cursor.fetchall()
    
    if not events:
        return None
    
    # Separate triggered event from custom field events
    triggered_event = None
    custom_field_events = []
    
    for event_type, payload in events:
        if event_type == 'incident.triggered':
            triggered_event = payload
        elif event_type == 'incident.custom_field_values.updated':
            custom_field_events.append(payload)
    
    # Use triggered event as base
    if not triggered_event:
        # Fallback to most recent event if no triggered event
        return events[-1][1] if events else None
    
    # If no custom fields to merge, return triggered event as-is
    if not custom_field_events:
        return triggered_event
    
    # Merge custom fields into the triggered event
    try:
        merged = json.loads(triggered_event) if isinstance(triggered_event, str) else triggered_event
        
        # Collect all custom fields from update events
        all_custom_fields = {}
        for cf_event in custom_field_events:
            cf_payload = json.loads(cf_event) if isinstance(cf_event, str) else cf_event
            cf_data = cf_payload.get('event', {}).get('data', {})
            for field in cf_data.get('custom_fields', []):
                field_name = field.get('name')
                if field_name:
                    all_custom_fields[field_name] = field
        
        # Add merged custom fields to the payload
        if all_custom_fields and 'event' in merged:
            if 'custom_fields' not in merged['event']:
                merged['event']['custom_fields'] = {}
            merged['event']['custom_fields'].update(all_custom_fields)
            
            # Also add to data.customFields for easier access in incident object
            if 'data' in merged['event']:
                if 'customFields' not in merged['event']['data']:
                    merged['event']['data']['customFields'] = {}
                # Convert to simple key-value format
                for field_name, field_data in all_custom_fields.items():
                    merged['event']['data']['customFields'][field_name] = field_data.get('value')
        
        return merged
        
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(
            "[PAGERDUTY] Failed to merge custom fields for incident %s: %s",
            incident_id,
            str(e)
        )
        return triggered_event


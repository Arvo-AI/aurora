"""The option catalog behind the ServiceNow filter pickers.

Priorities and states are fixed choice lists. Assignment groups and categories
come from the instance; when the connected account cannot read the catalog
table, the values seen on recent incidents stand in.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from routes.servicenow.rca_filters import PRIORITY_LABELS, PRIORITY_VALUES, STATE_LABELS, STATE_VALUES
from routes.servicenow.servicenow_client import ServiceNowAPIError

logger = logging.getLogger(__name__)

_LOG = "[SERVICENOW][FIELD-OPTIONS]"


def _catalog(primary: Callable[[], List[str]], fallback: Callable[[], List[str]], label: str) -> List[str]:
    """Catalog table first; values on recent incidents when that read is refused."""
    try:
        return primary()
    except (ServiceNowAPIError, ValueError) as exc:
        logger.info("%s %s catalog unavailable (%s); using values on recent incidents", _LOG, label, exc)
    return fallback()


def build_field_options(client: Any) -> Dict[str, Any]:
    """Everything the rules editor lists. ``errors`` names the catalogs that failed."""
    payload: Dict[str, Any] = {
        "priorities": [{"value": value, "label": PRIORITY_LABELS[value]} for value in PRIORITY_VALUES],
        "states": [{"value": value, "label": STATE_LABELS[value]} for value in STATE_VALUES],
        "assignmentGroups": [],
        "categories": [],
        "errors": [],
    }
    try:
        payload["assignmentGroups"] = _catalog(
            client.list_assignment_groups,
            lambda: client.distinct_incident_values("assignment_group"),
            "assignment group",
        )
    except Exception:
        logger.exception("%s Assignment groups failed", _LOG)
        payload["errors"].append("assignmentGroups")
    try:
        payload["categories"] = _catalog(
            client.list_categories,
            lambda: client.distinct_incident_values("category"),
            "category",
        )
    except Exception:
        logger.exception("%s Categories failed", _LOG)
        payload["errors"].append("categories")
    return payload

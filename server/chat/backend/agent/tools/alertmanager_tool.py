"""Alertmanager tool for the RCA agent.

Allows the agent to list active alerts, manage silences,
and query alert state during incident investigation.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.prometheus_connector.alertmanager_client import (
    AlertmanagerClient,
    AlertmanagerAPIError,
)
from connectors.prometheus_connector.base_client import build_auth_headers_from_creds
from routes.prometheus.prometheus_routes import _get_stored_prometheus_credentials

logger = logging.getLogger(__name__)

MAX_OUTPUT_SIZE = 50_000

_VALID_ACTIONS = (
    "list_alerts",
    "list_silences",
    "create_silence",
    "expire_silence",
)

_ACTION_HELP = ", ".join(f"'{a}'" for a in _VALID_ACTIONS)


class AlertmanagerToolArgs(BaseModel):
    action: str = Field(
        description=(
            "Action to perform. One of: "
            "'list_alerts' — get currently firing alerts from Alertmanager (includes silenced/inhibited state). "
            "'list_silences' — get active silences. "
            "'create_silence' — silence alerts matching given labels for a duration. "
            "'expire_silence' — remove an active silence by its ID."
        )
    )
    matchers: str = Field(
        default="",
        description=(
            "For list_alerts: comma-separated label filters, e.g. 'alertname=HighCPU,severity=critical'. "
            "For create_silence: REQUIRED — labels to match, e.g. 'alertname=HighCPU,namespace=production'. "
            "For list_silences/expire_silence: not used."
        ),
    )
    duration_minutes: int = Field(
        default=60,
        description="Duration of the silence in minutes (for create_silence). Max 1440 (24 hours). Default: 60.",
    )
    comment: str = Field(
        default="Silenced by Aurora during incident investigation",
        description="Reason for the silence (for create_silence). Helps team understand why it was silenced.",
    )
    silence_id: str = Field(
        default="",
        description="Silence ID to expire (for expire_silence action only).",
    )
    include_silenced: bool = Field(
        default=False,
        description="For list_alerts: include already-silenced alerts in the response. Default: false.",
    )


def _parse_matchers_string(matchers_str: str) -> List[str]:
    """Parse 'alertname=Foo,severity=critical' into Alertmanager filter format."""
    if not matchers_str.strip():
        return []
    parts = [m.strip() for m in matchers_str.split(",") if m.strip()]
    return parts


def _parse_matchers_for_silence(matchers_str: str) -> List[Dict[str, Any]]:
    """Parse 'alertname=Foo,severity=critical' into silence matcher objects."""
    if not matchers_str.strip():
        return []

    matchers = []
    for part in matchers_str.split(","):
        part = part.strip()
        if not part:
            continue

        is_regex = False
        is_equal = True

        # Regex not-equal
        if "!~" in part:
            name, value = part.split("!~", 1)
            is_regex = True
            is_equal = False
        # Regex equal
        elif "=~" in part:
            name, value = part.split("=~", 1)
            is_regex = True
        # Not equal
        elif "!=" in part:
            name, value = part.split("!=", 1)
            is_equal = False
        # Equal
        elif "=" in part:
            name, value = part.split("=", 1)
        else:
            continue

        matchers.append({
            "name": name.strip(),
            "value": value.strip().strip('"').strip("'"),
            "isRegex": is_regex,
            "isEqual": is_equal,
        })

    return matchers


def _get_alertmanager_client(user_id: str) -> tuple:
    """Build an AlertmanagerClient from stored credentials."""
    creds = _get_stored_prometheus_credentials(user_id)
    if not creds:
        return None, json.dumps({"error": "Prometheus not connected. Please connect Prometheus first."})

    alertmanager_url = creds.get("alertmanager_url")
    if not alertmanager_url:
        return None, json.dumps({
            "error": "Alertmanager URL not configured. "
                     "Please reconnect Prometheus and provide the Alertmanager URL."
        })

    try:
        auth_headers = build_auth_headers_from_creds(creds)
        client = AlertmanagerClient(
            alertmanager_url=alertmanager_url,
            auth_headers=auth_headers,
            verify_ssl=creds.get("verify_ssl", True),
        )
        return client, None
    except ValueError as exc:
        return None, json.dumps({"error": f"Invalid Alertmanager configuration: {exc}"})


def is_alertmanager_connected(user_id: str) -> bool:
    """Check if Alertmanager URL is configured for this user."""
    creds = _get_stored_prometheus_credentials(user_id)
    if not creds:
        return False
    return bool(creds.get("alertmanager_url"))


def manage_alertmanager(
    action: str,
    matchers: str = "",
    duration_minutes: int = 60,
    comment: str = "Silenced by Aurora during incident investigation",
    silence_id: str = "",
    include_silenced: bool = False,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Manage Alertmanager alerts and silences.

    Returns a JSON string with the result or error.
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client, err = _get_alertmanager_client(user_id)
    if err:
        return err

    action = action.lower().strip()
    if action not in _VALID_ACTIONS:
        return json.dumps({
            "error": f"Invalid action '{action}'. Must be one of: {_ACTION_HELP}",
        })

    logger.info(
        "[ALERTMANAGER-TOOL] user=%s action=%s matchers=%s",
        user_id, action, matchers[:100] if matchers else "",
    )

    try:
        if action == "list_alerts":
            filter_matchers = _parse_matchers_string(matchers) or None
            alerts = client.get_alerts(
                active=True,
                silenced=include_silenced,
                inhibited=False,
                filter_matchers=filter_matchers,
            )

            # Summarize for readability
            summary = []
            for alert in alerts[:200]:
                summary.append({
                    "alertname": alert.get("labels", {}).get("alertname"),
                    "severity": alert.get("labels", {}).get("severity"),
                    "status": alert.get("status", {}).get("state") if isinstance(alert.get("status"), dict) else alert.get("status"),
                    "labels": alert.get("labels"),
                    "annotations": alert.get("annotations"),
                    "startsAt": alert.get("startsAt"),
                    "fingerprint": alert.get("fingerprint"),
                })

            return json.dumps({
                "success": True,
                "action": "list_alerts",
                "count": len(alerts),
                "include_silenced": include_silenced,
                "alerts": summary,
            }, default=str)

        elif action == "list_silences":
            silences = client.get_silences()

            summary = []
            for silence in silences[:100]:
                summary.append({
                    "id": silence.get("id"),
                    "status": silence.get("status", {}).get("state"),
                    "matchers": silence.get("matchers"),
                    "createdBy": silence.get("createdBy"),
                    "comment": silence.get("comment"),
                    "startsAt": silence.get("startsAt"),
                    "endsAt": silence.get("endsAt"),
                })

            return json.dumps({
                "success": True,
                "action": "list_silences",
                "count": len(silences),
                "silences": summary,
            }, default=str)

        elif action == "create_silence":
            parsed_matchers = _parse_matchers_for_silence(matchers)
            if not parsed_matchers:
                return json.dumps({
                    "error": "matchers are required for create_silence. "
                             "Example: 'alertname=HighCPU,namespace=production'"
                })

            duration_minutes = min(max(duration_minutes, 1), 1440)

            result = client.create_silence(
                matchers=parsed_matchers,
                duration_minutes=duration_minutes,
                created_by="Aurora AI",
                comment=comment,
            )

            return json.dumps({
                "success": True,
                "action": "create_silence",
                "silence": result,
                "note": f"Silence created for {duration_minutes} minutes. "
                        f"Use expire_silence with ID '{result.get('silenceId')}' to remove early.",
            }, default=str)

        elif action == "expire_silence":
            if not silence_id.strip():
                return json.dumps({
                    "error": "silence_id is required for expire_silence. "
                             "Use list_silences to find active silence IDs."
                })

            client.expire_silence(silence_id.strip())

            return json.dumps({
                "success": True,
                "action": "expire_silence",
                "silence_id": silence_id.strip(),
                "note": "Silence expired successfully. Matching alerts will fire again.",
            })

        return json.dumps({"error": f"Unhandled action: {action}"})

    except AlertmanagerAPIError as exc:
        status = exc.status_code
        msg = str(exc)
        if status in (401, 403):
            return json.dumps({"error": "Alertmanager authentication failed. Credentials may be invalid."})
        return json.dumps({"error": f"Alertmanager API error: {msg[:300]}"})
    except Exception:
        logger.exception("[ALERTMANAGER-TOOL] Failed for user=%s action=%s", user_id, action)
        return json.dumps({"error": "Internal error while communicating with Alertmanager"})

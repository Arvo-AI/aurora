"""Splunk On-Call (VictorOps) API client and helper functions."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from flask import jsonify

logger = logging.getLogger(__name__)

VICTOROPS_API_BASE = "https://api.victorops.com/api-public/v1"


class VictorOpsAPIError(Exception):
    """VictorOps API error."""


class VictorOpsClient:
    """Splunk On-Call (VictorOps) REST API client.

    Authentication uses two headers:
        X-VO-Api-Id  — the API ID from the Splunk On-Call portal
        X-VO-Api-Key — the API Key from the Splunk On-Call portal
    """

    def __init__(self, api_id: str, api_key: str):
        self.api_id = api_id
        self.api_key = api_key
        self.base_url = VICTOROPS_API_BASE

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "X-VO-Api-Id": self.api_id,
            "X-VO-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                # (connect_timeout, read_timeout) — fail fast on DNS/connect, generous on read
                timeout=(5, 15),
                **kwargs,
            )
            response.raise_for_status()
            return response
        except requests.exceptions.ConnectTimeout:
            raise VictorOpsAPIError("Connection timed out reaching Splunk On-Call API. Check your network or try again.")
        except requests.exceptions.ReadTimeout:
            raise VictorOpsAPIError("Splunk On-Call API took too long to respond. Please try again.")
        except requests.exceptions.ConnectionError as e:
            raise VictorOpsAPIError(f"Could not connect to Splunk On-Call API: {e}")
        except requests.RequestException as e:
            if hasattr(e, "response") and e.response is not None:
                status_code = e.response.status_code
                if status_code == 429:
                    raise VictorOpsAPIError("Rate limited by Splunk On-Call")
                elif status_code == 401:
                    raise VictorOpsAPIError("Unauthorized: Invalid API ID or Key")
                elif status_code == 403:
                    raise VictorOpsAPIError("Forbidden: API credentials lack required permissions")
                elif status_code == 404:
                    raise VictorOpsAPIError("Not found: check your API ID and Key")
                else:
                    raise VictorOpsAPIError(str(e))
            else:
                raise VictorOpsAPIError(str(e))

    def get_current_oncall(self) -> Dict[str, Any]:
        """Lightweight endpoint — used only for credential validation."""
        return self._request("GET", "/oncall/current").json()

    def get_teams(self) -> Dict[str, Any]:
        """Fetch all teams."""
        return self._request("GET", "/team").json()

    def get_incidents(self, limit: int = 10) -> Dict[str, Any]:
        """Fetch recent incidents."""
        return self._request("GET", f"/incidents?limit={limit}").json()


def validate_credentials(client: VictorOpsClient) -> Dict[str, Any]:
    """Validate API credentials using the lightweight /oncall/current endpoint."""
    result = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "capabilities": {"can_read_incidents": True},
    }

    data = client.get_current_oncall()
    # teamsOnCall is present in a valid response
    if isinstance(data, dict):
        teams = data.get("teamsOnCall", [])
        if teams and isinstance(teams, list):
            first_team = teams[0]
            if team_name := first_team.get("team", {}).get("name"):
                result["account_name"] = team_name

    return result


def error_response(exc: VictorOpsAPIError):
    """Convert a VictorOpsAPIError to an HTTP response."""
    msg = str(exc).lower()

    if "timed out" in msg or "too long" in msg:
        return jsonify({"error": str(exc)}), 504
    if "could not connect" in msg:
        return jsonify({"error": str(exc)}), 502
    if "unauthorized" in msg or "invalid api" in msg:
        return jsonify({"error": "Invalid API ID or Key"}), 401
    if "forbidden" in msg:
        return jsonify({"error": "API credentials lack required permissions"}), 403
    if "rate limit" in msg:
        return jsonify({"error": "Rate limited by Splunk On-Call"}), 429
    if "not found" in msg:
        return jsonify({"error": "Not found: check your API ID"}), 404

    logger.error("Splunk On-Call API error: %s", exc)
    return jsonify({"error": "Splunk On-Call API request failed"}), 502

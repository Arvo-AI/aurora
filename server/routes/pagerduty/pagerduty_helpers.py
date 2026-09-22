"""PagerDuty API client and helper functions."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from flask import jsonify

logger = logging.getLogger(__name__)


class PagerDutyAPIError(Exception):
    """PagerDuty API error.

    status_code is the HTTP status PagerDuty answered with, or None when no
    response arrived (timeout, connection reset): the request's outcome is
    then unknown to the caller.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# Base roles that cannot modify incidents (add notes). Observer/restricted_access
# may hold object-level write roles, but /users/me does not expose those, so
# they are denied here; a team-level restriction on a write role surfaces as a
# 403 at post time instead.
PD_READ_ONLY_ROLES = frozenset({"read_only_user", "read_only_limited_user", "observer", "restricted_access"})


class PagerDutyClient:
    """PagerDuty API client."""
    
    def __init__(self, api_token: str = None, oauth_token: str = None, from_email: str = None):
        self.token = oauth_token if oauth_token else api_token
        self.is_oauth = bool(oauth_token)
        self.from_email = from_email
        self.base_url = "https://api.pagerduty.com"
    
    @property
    def headers(self) -> Dict[str, str]:
        auth = f"Bearer {self.token}" if self.is_oauth else f"Token token={self.token}"
        headers = {
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Authorization": auth,
        }
        if self.from_email:
            # Required by write endpoints (e.g. create note): the user recorded as the actor
            headers["From"] = self.from_email
        return headers
    
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = requests.request(method, f"{self.base_url}{path}", headers=self.headers, timeout=20, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                status_code = e.response.status_code
                if status_code == 429:
                    raise PagerDutyAPIError("Rate limited", status_code)
                elif status_code == 401:
                    raise PagerDutyAPIError("Unauthorized: Invalid or expired API token", status_code)
                elif status_code == 400:
                    # Extract error message for account-level token detection
                    try:
                        error_detail = e.response.json()
                        if isinstance(error_detail.get('error'), str):
                            error_msg = error_detail.get('error')
                        elif isinstance(error_detail.get('error'), dict):
                            error_msg = error_detail.get('error', {}).get('message', 'Bad Request')
                        else:
                            error_msg = 'Bad Request'
                    except (ValueError, KeyError):
                        error_msg = 'Bad Request: Invalid token format'
                    raise PagerDutyAPIError(error_msg, status_code)
                elif status_code == 403:
                    raise PagerDutyAPIError("Forbidden: Token lacks required permissions", status_code)
                else:
                    raise PagerDutyAPIError(str(e), status_code)
            else:
                raise PagerDutyAPIError(str(e))
    
    def get_current_user(self) -> Dict[str, Any]:
        return self._request("GET", "/users/me").json()
    
    def create_note(self, incident_id: str, content: str) -> Dict[str, Any]:
        """Add a note to an incident (needs incidents.write and a From header).

        Notes are immutable: PagerDuty has no edit or delete endpoint, so the
        caller owns idempotency (one note per Aurora incident).
        """
        return self._request(
            "POST", f"/incidents/{incident_id}/notes", json={"note": {"content": content}}
        ).json()
    
    def get_subdomain(self) -> Optional[str]:
        try:
            services = self._request("GET", "/services?limit=1").json().get("services", [])
            if services:
                url = services[0].get("html_url", "")
                parsed = urlparse(url)
                if parsed.hostname and parsed.hostname.endswith(".pagerduty.com"):
                    return parsed.hostname.replace(".pagerduty.com", "")
        except Exception:
            return None


def validate_token(client: PagerDutyClient, granted_scopes: Optional[str] = None) -> Dict[str, Any]:
    """Validate token and extract info, including whether it can write incidents.

    can_write_incidents is default-deny: True only for a user-scoped key (or
    OAuth token) whose /users/me role is not read-only. OAuth additionally
    needs `incidents.write` in granted_scopes (the space-separated `scope`
    from the token response); passing None denies, so a connectivity check
    can never upgrade a stored capability. Account-level keys cannot be
    introspected (no /users/me) and are reported as api_key_access="account".
    """
    capabilities = {
        "can_read_incidents": True,
        "can_write_incidents": False,
        "api_key_access": "oauth" if client.is_oauth else "user",
    }
    result = {"validated_at": datetime.now(timezone.utc).isoformat(), "capabilities": capabilities}
    
    try:
        user = client.get_current_user().get("user", {})
        if email := user.get("email"):
            result["external_user_email"] = email
        if name := (user.get("name") or user.get("summary")):
            result["external_user_name"] = name
        if url := user.get("html_url"):
            parsed_url = urlparse(url)
            if parsed_url.hostname and parsed_url.hostname.endswith(".pagerduty.com"):
                result["account_subdomain"] = parsed_url.hostname.replace(".pagerduty.com", "")
        role = user.get("role")
        if role:
            result["external_user_role"] = role
        role_ok = bool(role) and role not in PD_READ_ONLY_ROLES
        if client.is_oauth:
            capabilities["can_write_incidents"] = role_ok and "incidents.write" in (granted_scopes or "").split()
        else:
            capabilities["can_write_incidents"] = role_ok
    except PagerDutyAPIError as e:
        error_msg = str(e).lower()
        if "account-level" in error_msg or "user's identity" in error_msg:
            capabilities["api_key_access"] = "account"
            if subdomain := client.get_subdomain():
                result["account_subdomain"] = subdomain
        else:
            raise
    
    return result


def error_response(exc: PagerDutyAPIError):
    """Convert PagerDutyAPIError to HTTP response."""
    msg = str(exc).lower()
    
    if "unauthorized" in msg or "invalid or expired" in msg:
        return jsonify({"error": "Invalid or expired API token"}), 401
    if "bad request" in msg or "invalid token format" in msg:
        return jsonify({"error": "Invalid token format"}), 400
    if "forbidden" in msg:
        return jsonify({"error": "Token lacks required permissions"}), 403
    if "rate limit" in msg:
        return jsonify({"error": "Rate limited by PagerDuty"}), 429
    
    logger.error(f"PagerDuty API error: {exc}")
    return jsonify({"error": "PagerDuty API request failed"}), 502


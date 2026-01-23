"""PagerDuty API client and helper functions."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from flask import jsonify

logger = logging.getLogger(__name__)


class PagerDutyAPIError(Exception):
    """PagerDuty API error."""


class PagerDutyClient:
    """PagerDuty API client."""
    
    def __init__(self, api_token: str = None, oauth_token: str = None):
        self.token = oauth_token if oauth_token else api_token
        self.is_oauth = bool(oauth_token)
        self.base_url = "https://api.pagerduty.com"
    
    @property
    def headers(self) -> Dict[str, str]:
        auth = f"Bearer {self.token}" if self.is_oauth else f"Token token={self.token}"
        return {
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Authorization": auth,
        }
    
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = requests.request(method, f"{self.base_url}{path}", headers=self.headers, timeout=20, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                status_code = e.response.status_code
                if status_code == 429:
                    raise PagerDutyAPIError("Rate limited")
                elif status_code == 401:
                    raise PagerDutyAPIError("Unauthorized: Invalid or expired API token")
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
                    raise PagerDutyAPIError(error_msg)
                elif status_code == 403:
                    raise PagerDutyAPIError("Forbidden: Token lacks required permissions")
                else:
                    raise PagerDutyAPIError(str(e))
            else:
                raise PagerDutyAPIError(str(e))
    
    def get_current_user(self) -> Dict[str, Any]:
        return self._request("GET", "/users/me").json()
    
    def get_subdomain(self) -> Optional[str]:
        try:
            services = self._request("GET", "/services?limit=1").json().get("services", [])
            if services and ".pagerduty.com" in (url := services[0].get("html_url", "")):
                return url.split("://")[1].split(".pagerduty.com")[0]
        except Exception:
            return None
    
    def can_write(self) -> bool:
        try:
            r = requests.post(f"{self.base_url}/incidents", headers=self.headers, json={"incident": {"type": "incident"}}, timeout=20)
            return r.status_code != 403
        except Exception:
            return False


def validate_token(client: PagerDutyClient) -> Dict[str, Any]:
    """Validate token and extract info."""
    result = {"validated_at": datetime.now(timezone.utc).isoformat(), "capabilities": {"can_read_incidents": True, "can_write_incidents": client.can_write()}}
    
    try:
        user = client.get_current_user().get("user", {})
        if email := user.get("email"):
            result["external_user_email"] = email
        if name := (user.get("name") or user.get("summary")):
            result["external_user_name"] = name
        if url := user.get("html_url"):
            if ".pagerduty.com" in url:
                result["account_subdomain"] = url.split("://")[1].split(".pagerduty.com")[0]
    except PagerDutyAPIError as e:
        error_msg = str(e).lower()
        if "account-level" in error_msg or "user's identity" in error_msg:
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
    
    return jsonify({"error": str(exc)}), 502


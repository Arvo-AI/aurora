"""Microsoft Graph client for Teams channel operations."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class TeamsAPIError(ValueError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class TeamsClient:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params=None, json_body=None, timeout=30) -> Dict[str, Any]:
        url = f"{GRAPH_BASE}{path}"
        for attempt in range(3):
            response = requests.request(
                method, url, headers=self.headers, params=params, json=json_body, timeout=timeout,
            )
            if response.status_code == 429 and attempt < 2:
                retry = int(response.headers.get("Retry-After", 2 * (attempt + 1)))
                time.sleep(min(retry, 30))
                continue
            if not response.ok:
                raise TeamsAPIError(
                    f"Graph API error {response.status_code} on {path}",
                    status_code=response.status_code,
                )
            if response.status_code == 204:
                return {}
            return response.json()
        raise TeamsAPIError(f"Graph API rate limited on {path}", status_code=429)

    def list_joined_teams(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/me/joinedTeams")
        return data.get("value") or []

    def list_team_channels(self, team_id: str) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/teams/{team_id}/channels")
        return data.get("value") or []

    def get_channel(self, team_id: str, channel_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/teams/{team_id}/channels/{channel_id}")

    def list_message_replies(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        data = self._request(
            "GET",
            f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
            params={"$top": max(1, min(limit, 50))},
        )
        return data.get("value") or []

    def list_channel_messages(self, team_id: str, channel_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        data = self._request(
            "GET",
            f"/teams/{team_id}/channels/{channel_id}/messages",
            params={"$top": max(1, min(limit, 50))},
        )
        return data.get("value") or []

    def send_channel_message(
        self,
        team_id: str,
        channel_id: str,
        text: str,
        reply_to_id: Optional[str] = None,
        *,
        content_type: str = "text",
    ) -> Dict[str, Any]:
        ctype = "html" if content_type == "html" else "text"
        body: Dict[str, Any] = {"body": {"contentType": ctype, "content": text}}
        if reply_to_id:
            return self._request(
                "POST",
                f"/teams/{team_id}/channels/{channel_id}/messages/{reply_to_id}/replies",
                json_body=body,
            )
        return self._request(
            "POST",
            f"/teams/{team_id}/channels/{channel_id}/messages",
            json_body=body,
        )

    def get_me(self) -> Dict[str, Any]:
        return self._request("GET", "/me")


def get_teams_client_for_user(user_id: str) -> Optional[TeamsClient]:
    try:
        from utils.auth.stateless_auth import get_credentials_from_db

        creds = get_credentials_from_db(user_id, "teams")
        if not creds or not creds.get("access_token"):
            return None
        return TeamsClient(creds["access_token"])
    except Exception:
        logger.exception("Failed to get Teams client")
        return None

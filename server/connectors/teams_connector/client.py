"""Microsoft Graph client for Teams channel operations."""

from __future__ import annotations

import logging
import posixpath
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Graph resource ids (teams GUIDs, channel ids like 19:...@thread.tacv2, message ids).
_GRAPH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")


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

    @staticmethod
    def _validate_graph_path(path: str) -> None:
        """Reject traversal / absolute URLs in paths passed to requests."""
        if not path.startswith("/"):
            raise TeamsAPIError("Invalid Graph path: must start with /")
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc:
            raise TeamsAPIError("Invalid Graph path: must be relative")
        if parsed.query or parsed.fragment:
            raise TeamsAPIError("Invalid Graph path: query or fragment not allowed")
        decoded = unquote(parsed.path)
        if "/.." in decoded or decoded.startswith("//"):
            raise TeamsAPIError("Invalid Graph path: traversal not allowed")
        if posixpath.normpath(decoded) != decoded:
            raise TeamsAPIError("Invalid Graph path: non-canonical segments")

    @staticmethod
    def _graph_url(path: str) -> str:
        TeamsClient._validate_graph_path(path)
        url = urljoin(f"{GRAPH_BASE.rstrip('/')}/", path.lstrip("/"))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com":
            raise TeamsAPIError("Invalid Graph URL")
        return url

    @staticmethod
    def _safe_segment(value: str, *, label: str) -> str:
        if not value or len(value) > 512:
            raise TeamsAPIError(f"Invalid Graph {label}")
        if not _GRAPH_SEGMENT_RE.match(value):
            raise TeamsAPIError(f"Invalid Graph {label}")
        return value

    def _request(self, method: str, path: str, *, params=None, json_body=None, timeout=30) -> Dict[str, Any]:
        url = self._graph_url(path)
        for attempt in range(3):
            response = requests.request(
                method, url, headers=self.headers, params=params, json=json_body, timeout=timeout,
            )
            if response.status_code == 429 and attempt < 2:
                try:
                    retry = int(response.headers.get("Retry-After", 2 * (attempt + 1)))
                except (TypeError, ValueError):
                    retry = 2 * (attempt + 1)
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
        tid = self._safe_segment(team_id, label="team_id")
        data = self._request("GET", f"/teams/{tid}/channels")
        return data.get("value") or []

    def get_channel(self, team_id: str, channel_id: str) -> Dict[str, Any]:
        tid = self._safe_segment(team_id, label="team_id")
        cid = self._safe_segment(channel_id, label="channel_id")
        return self._request("GET", f"/teams/{tid}/channels/{cid}")

    def list_message_replies(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        tid = self._safe_segment(team_id, label="team_id")
        cid = self._safe_segment(channel_id, label="channel_id")
        mid = self._safe_segment(message_id, label="message_id")
        data = self._request(
            "GET",
            f"/teams/{tid}/channels/{cid}/messages/{mid}/replies",
            params={"$top": max(1, min(limit, 50))},
        )
        return data.get("value") or []

    def list_channel_messages(self, team_id: str, channel_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        tid = self._safe_segment(team_id, label="team_id")
        cid = self._safe_segment(channel_id, label="channel_id")
        data = self._request(
            "GET",
            f"/teams/{tid}/channels/{cid}/messages",
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
        """Graph delegated send — do not use for user-visible Aurora messages; use bot_client."""
        tid = self._safe_segment(team_id, label="team_id")
        cid = self._safe_segment(channel_id, label="channel_id")
        ctype = "html" if content_type == "html" else "text"
        body: Dict[str, Any] = {"body": {"contentType": ctype, "content": text}}
        if reply_to_id:
            rid = self._safe_segment(reply_to_id, label="message_id")
            return self._request(
                "POST",
                f"/teams/{tid}/channels/{cid}/messages/{rid}/replies",
                json_body=body,
            )
        return self._request(
            "POST",
            f"/teams/{tid}/channels/{cid}/messages",
            json_body=body,
        )

    def get_me(self) -> Dict[str, Any]:
        return self._request("GET", "/me")


def _access_token_expired(creds: Dict[str, Any]) -> bool:
    expires_in = creds.get("expires_in")
    connected_at = creds.get("connected_at")
    if not expires_in or not connected_at:
        return False
    try:
        expiry = int(connected_at) + int(expires_in) - 300
    except (TypeError, ValueError):
        return False
    return time.time() >= expiry


def get_teams_client_for_user(user_id: str) -> Optional[TeamsClient]:
    try:
        from connectors.teams_connector.oauth import refresh_access_token
        from utils.auth.stateless_auth import get_credentials_from_db
        from utils.auth.token_management import store_tokens_in_db

        creds = get_credentials_from_db(user_id, "teams")
        if not creds or not creds.get("access_token"):
            return None
        access_token = creds["access_token"]
        if _access_token_expired(creds):
            refresh = creds.get("refresh_token")
            if not refresh:
                logger.warning("Teams access token expired and no refresh token for user")
                return None
            token_data = refresh_access_token(refresh)
            access_token = token_data.get("access_token")
            if not access_token:
                return None
            creds["access_token"] = access_token
            if token_data.get("refresh_token"):
                creds["refresh_token"] = token_data["refresh_token"]
            if token_data.get("expires_in") is not None:
                creds["expires_in"] = token_data["expires_in"]
            creds["connected_at"] = int(time.time())
            store_tokens_in_db(user_id, creds, "teams")
        return TeamsClient(access_token)
    except Exception:
        logger.exception("Failed to get Teams client")
        return None

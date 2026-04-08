"""
Google Chat API client for Aurora integration.
Handles message sending, space management, and message reading.

Uses the Google Chat REST API (v1) with service account or user OAuth credentials.
"""

import logging
import requests
import json
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

GOOGLE_CHAT_API_BASE = "https://chat.googleapis.com/v1"


class GoogleChatClient:
    """
    Google Chat API client for Aurora integration.
    Handles message sending, space listing, and message reading.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _request(
        self, method: str, path: str, json_data: Optional[Dict] = None,
        params: Optional[Dict] = None, timeout: int = 30,
    ) -> Dict[str, Any]:
        """Make a request to the Google Chat API."""
        url = f"{GOOGLE_CHAT_API_BASE}/{path}"

        try:
            response = self._session.request(
                method, url, json=json_data, params=params, timeout=timeout,
            )
            response.raise_for_status()
            if response.status_code == 204:
                return {"ok": True}
            return response.json()

        except requests.HTTPError as e:
            error_body = {}
            try:
                error_body = e.response.json() if e.response is not None else {}
            except Exception:
                pass
            error_msg = (
                error_body.get("error", {}).get("message")
                or str(e)
            )
            logger.error(f"Google Chat API error on {path}: {error_msg}")
            raise
        except requests.RequestException as e:
            logger.error(f"Request to Google Chat API failed: {e}")
            raise ValueError(f"Failed to communicate with Google Chat: {str(e)}")

    # ── Messages ────────────────────────────────────────────────────

    def send_message(
        self, space_name: str, text: str, thread_key: Optional[str] = None,
        cards_v2: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Send a message to a Google Chat space."""
        body: Dict[str, Any] = {"text": text}
        params: Dict[str, str] = {}

        if thread_key:
            body["thread"] = {"threadKey": thread_key}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        if cards_v2:
            body["cardsV2"] = cards_v2

        result = self._request("POST", f"{space_name}/messages", body, params=params)
        logger.info(f"Message sent to {space_name}: {result.get('name')}")
        return result

    def send_private_message(
        self, space_name: str, text: str, user_name: str,
        thread_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a private message visible only to one user (like Slack ephemeral)."""
        body: Dict[str, Any] = {
            "text": text,
            "privateMessageViewer": {"name": user_name},
        }
        if thread_key:
            body["thread"] = {"threadKey": thread_key}

        params = {"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"} if thread_key else {}
        return self._request("POST", f"{space_name}/messages", body, params=params)

    def update_message(
        self, message_name: str, text: str,
        cards_v2: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Update an existing message."""
        body: Dict[str, Any] = {"text": text}
        update_fields = ["text"]

        if cards_v2:
            body["cardsV2"] = cards_v2
            update_fields.append("cardsV2")

        params = {"updateMask": ",".join(update_fields)}
        return self._request("PATCH", message_name, body, params=params)

    # ── Spaces ──────────────────────────────────────────────────────

    def list_spaces(self) -> List[Dict[str, Any]]:
        """List spaces the bot is a member of (with pagination)."""
        all_spaces = []
        page_token = None

        while True:
            params: Dict[str, Any] = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            result = self._request("GET", "spaces", params=params)
            spaces = result.get("spaces", [])
            all_spaces.extend(spaces)

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return all_spaces

    def create_space(self, display_name: str, space_type: str = "SPACE") -> Dict[str, Any]:
        """Create a new space."""
        body = {
            "spaceType": space_type,
            "displayName": display_name,
        }
        result = self._request("POST", "spaces", body)
        logger.info(f"Created space: {display_name} ({result.get('name')})")
        return result

    def get_space(self, space_name: str) -> Dict[str, Any]:
        """Get details of a space."""
        return self._request("GET", space_name)

    def update_space(self, space_name: str, description: str) -> Dict[str, Any]:
        """Update space description."""
        body = {"spaceDetails": {"description": description}}
        params = {"updateMask": "spaceDetails"}
        return self._request("PATCH", space_name, body, params=params)

    def find_space_by_name(self, display_name: str) -> Optional[Dict[str, Any]]:
        """Find a space by display name."""
        for space in self.list_spaces():
            if space.get("displayName") == display_name:
                return space
        return None

    # ── Members ─────────────────────────────────────────────────────

    def add_member(self, space_name: str, user_email: str) -> Optional[Dict[str, Any]]:
        """Add a user to a space by email."""
        try:
            body = {
                "member": {
                    "name": f"users/{user_email}",
                    "type": "HUMAN",
                },
            }
            result = self._request("POST", f"{space_name}/members", body)
            logger.info(f"Added {user_email} to {space_name}")
            return result
        except Exception as e:
            logger.warning(f"Could not add {user_email} to {space_name}: {e}")
            return None

    # ── History ─────────────────────────────────────────────────────

    def list_messages(
        self, space_name: str, page_size: int = 25, order_by: str = "createTime desc",
    ) -> List[Dict[str, Any]]:
        """List recent messages in a space."""
        params = {"pageSize": page_size, "orderBy": order_by}
        result = self._request("GET", f"{space_name}/messages", params=params)
        return result.get("messages", [])

    def get_message(self, message_name: str) -> Dict[str, Any]:
        """Get a single message by name."""
        return self._request("GET", message_name)

    # ── Auth test ───────────────────────────────────────────────────

    def test_auth(self) -> Dict[str, Any]:
        """Validate credentials by listing spaces (lightweight call)."""
        params = {"pageSize": 1}
        return self._request("GET", "spaces", params=params)


def create_incidents_space(
    access_token: str, org_display_name: str, installer_email: str,
) -> Dict[str, Any]:
    """
    Find or create the incidents space for Aurora notifications.

    Logic mirrors the Slack connector:
    1. Try to find existing 'Incidents' space the bot is in
    2. If found, use it
    3. If not found, create 'Aurora Incidents' space
    """
    try:
        client = GoogleChatClient(access_token)
        space_display_name = "Incidents"
        space_name = None
        created = False
        message = ""

        existing = client.find_space_by_name(space_display_name)
        if existing:
            space_name = existing["name"]
            message = f"Using existing space: {space_display_name}"
        else:
            space_display_name = "Aurora Incidents"
            existing_aurora = client.find_space_by_name(space_display_name)
            if existing_aurora:
                space_name = existing_aurora["name"]
                message = f"Using existing space: {space_display_name}"
            else:
                try:
                    new_space = client.create_space(space_display_name)
                    space_name = new_space["name"]
                    created = True
                    message = f"Created space: {space_display_name}"
                except Exception as e:
                    logger.error(f"Failed to create space: {e}")
                    return {"ok": False, "error": "Failed to create Google Chat space"}

        if space_name:
            try:
                if installer_email:
                    client.add_member(space_name, installer_email)
                if created:
                    client.update_space(
                        space_name, "Aurora incident alerts and notifications"
                    )
                    client.send_message(
                        space_name,
                        (
                            f"Welcome to {space_display_name}!\n\n"
                            f"Aurora is now connected to {org_display_name}. "
                            "This space will be used for:\n\n"
                            "• Real-time incident alerts and notifications\n"
                            "• Automated root cause analysis updates\n\n"
                            "Mention @Aurora in any space to start a conversation!"
                        ),
                    )
            except Exception as setup_e:
                logger.warning(f"Error during space setup: {setup_e}")

            return {
                "ok": True,
                "space_name": space_name,
                "space_display_name": space_display_name,
                "created": created,
                "message": message,
            }

        return {"ok": False, "error": "Failed to resolve incidents space"}

    except Exception as e:
        logger.error(f"Failed to create incidents space: {e}", exc_info=True)
        return {"ok": False, "error": "Failed to set up incidents space"}


def get_google_chat_client_for_user(user_id: str) -> Optional[GoogleChatClient]:
    """
    Get authenticated Google Chat client for a user (org-scoped via get_credentials_from_db).

    Attempts to use the stored access token. If it fails with a 401,
    refreshes the token using the stored refresh_token, persists the
    new credentials, and returns a client with the fresh token.
    """
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        from utils.auth.token_management import store_tokens_in_db

        creds = get_credentials_from_db(user_id, "google_chat")
        if not creds or not creds.get("access_token"):
            logger.debug(f"No Google Chat credentials for user {user_id}")
            return None

        client = GoogleChatClient(creds["access_token"])

        try:
            client.test_auth()
            return client
        except Exception:
            pass

        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning(f"Google Chat token expired and no refresh_token for user {user_id}")
            return None

        try:
            from connectors.google_chat_connector.oauth import refresh_access_token

            token_data = refresh_access_token(refresh_token)
            new_access = token_data.get("access_token")
            if not new_access:
                logger.error("Google Chat token refresh returned no access_token")
                return None

            updated = dict(creds)
            updated["access_token"] = new_access
            if token_data.get("refresh_token"):
                updated["refresh_token"] = token_data["refresh_token"]

            store_tokens_in_db(user_id, updated, "google_chat")
            logger.info(f"Refreshed Google Chat token for user {user_id}")

            return GoogleChatClient(new_access)
        except Exception as e:
            logger.error(f"Failed to refresh Google Chat token for user {user_id}: {e}", exc_info=True)
            return None

    except Exception as e:
        logger.error(f"Failed to get Google Chat client for user {user_id}: {e}", exc_info=True)
        return None

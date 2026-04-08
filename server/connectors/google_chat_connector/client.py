"""
Google Chat API client for Aurora integration.
Handles message sending, space management, and message reading.

Hybrid auth model:
  - User OAuth is used during *setup* to create/find the incidents space
    inside the customer's Google Workspace. Tokens are used once and discarded.
  - The Chat app service account is required for all ongoing messaging so that
    messages appear as "Aurora", not as the connecting user.
"""

import logging
import os
import json
import threading
import requests
from typing import Dict, Any, List, Optional

from google.oauth2 import service_account as google_service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)

GOOGLE_CHAT_API_BASE = "https://chat.googleapis.com/v1"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"

_sa_credentials: Optional[google_service_account.Credentials] = None
_sa_lock = threading.Lock()


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

    def add_app(self, space_name: str) -> Optional[Dict[str, Any]]:
        """Add the Chat app to a space (requires user auth with chat.memberships.app scope)."""
        try:
            body = {
                "member": {
                    "name": "users/app",
                    "type": "BOT",
                },
            }
            result = self._request("POST", f"{space_name}/members", body)
            logger.info(f"Added Chat app to {space_name}")
            return result
        except Exception as e:
            logger.warning(f"Could not add Chat app to {space_name}: {e}")
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


def _load_service_account_credentials() -> Optional[google_service_account.Credentials]:
    """Load and cache Google Chat service account credentials.

    Reads from GOOGLE_CHAT_SERVICE_ACCOUNT_KEY (inline JSON).
    """
    global _sa_credentials
    with _sa_lock:
        if _sa_credentials is not None and _sa_credentials.valid:
            return _sa_credentials

        key_json = os.getenv("GOOGLE_CHAT_SERVICE_ACCOUNT_KEY")

        try:
            if key_json:
                info = json.loads(key_json)
                creds = google_service_account.Credentials.from_service_account_info(
                    info, scopes=[CHAT_BOT_SCOPE],
                )
            else:
                logger.debug("No Google Chat service account configured")
                return None

            creds.refresh(GoogleAuthRequest())
            _sa_credentials = creds
            return creds
        except Exception as e:
            logger.error(f"Failed to load Google Chat service account: {e}", exc_info=True)
            return None


def get_chat_app_client() -> Optional["GoogleChatClient"]:
    """Get a GoogleChatClient authenticated as the Chat app (service account).

    Messages sent via this client appear as "Aurora" in Google Chat.
    """
    creds = _load_service_account_credentials()
    if not creds:
        return None

    if not creds.valid:
        try:
            creds.refresh(GoogleAuthRequest())
        except Exception as e:
            logger.error(f"Failed to refresh service account token: {e}", exc_info=True)
            return None

    return GoogleChatClient(creds.token)


def create_incidents_space(access_token: str) -> Dict[str, Any]:
    """
    Find or create the incidents space for Aurora notifications.

    Uses the *user's* OAuth token so the space is created inside the
    customer's Google Workspace.  Also adds the Chat app to the space
    so the service account can send messages as "Aurora" going forward.
    """
    try:
        user_client = GoogleChatClient(access_token)
        space_display_name = "Incidents"
        space_name = None
        created = False
        message = ""

        existing = user_client.find_space_by_name(space_display_name)
        if existing:
            space_name = existing["name"]
            message = f"Using existing space: {space_display_name}"
        else:
            space_display_name = "Aurora Incidents"
            existing_aurora = user_client.find_space_by_name(space_display_name)
            if existing_aurora:
                space_name = existing_aurora["name"]
                message = f"Using existing space: {space_display_name}"
            else:
                try:
                    new_space = user_client.create_space(space_display_name)
                    space_name = new_space["name"]
                    created = True
                    message = f"Created space: {space_display_name}"
                except requests.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 0
                    if status in (403, 401):
                        logger.error(f"Permission denied creating space: {e}")
                        return {"ok": False, "error": "insufficient_permissions"}
                    logger.error(f"Failed to create space: {e}")
                    return {"ok": False, "error": "space_creation_failed"}
                except Exception as e:
                    logger.error(f"Failed to create space: {e}")
                    return {"ok": False, "error": "space_creation_failed"}

        if space_name:
            try:
                user_client.add_app(space_name)

                if created:
                    user_client.update_space(
                        space_name, "Aurora incident alerts and notifications"
                    )

                app_client = get_chat_app_client()
                if app_client:
                    app_client.send_message(
                        space_name,
                        (
                            f"Welcome to {space_display_name}!\n\n"
                            "Aurora is now connected. "
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

        return {"ok": False, "error": "space_not_resolved"}

    except Exception as e:
        logger.error(f"Failed to create incidents space: {e}", exc_info=True)
        return {"ok": False, "error": "setup_failed"}

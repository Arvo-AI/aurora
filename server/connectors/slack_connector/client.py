"""
Slack API client for sending and reading messages.
Uses the stored access_token from OAuth to interact with Slack workspace.
"""

import logging
import time
import requests
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


class SlackClient:
    """
    Slack API client for Aurora integration.
    Handles message sending, channel listing, and message reading.
    """
    
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
    
    @staticmethod
    def _get_retry_delay(attempt: int, response=None) -> int:
        """Parse Retry-After header or compute exponential backoff, capped at 30s."""
        fallback = 2 * (attempt + 1)
        if response is not None:
            try:
                delay = int(response.headers.get("Retry-After", fallback))
            except (TypeError, ValueError):
                delay = fallback
        else:
            delay = fallback
        return min(delay, 30)

    def _validate_response(self, result: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
        """Check Slack's ok field; raise ValueError on API-level errors."""
        if result.get('ok', False):
            return result
        error = result.get('error', 'unknown_error')
        if not (error == 'name_taken' and endpoint == 'conversations.create'):
            logger.error("Slack API error on %s: %s", endpoint, error)
        raise ValueError(f"Slack API error: {error}")

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None, timeout: int = 30, max_retries: int = 3) -> Dict[str, Any]:
        """Make a request to Slack API with retry on 429 rate limits."""
        url = f"{SLACK_API_BASE}/{endpoint}"
        
        for attempt in range(max_retries + 1):
            try:
                if method == "GET":
                    response = requests.get(url, headers=self.headers, params=data, timeout=timeout)
                else:
                    response = requests.post(url, headers=self.headers, json=data, timeout=timeout)
                
                if response.status_code == 429 and attempt < max_retries:
                    retry_after = self._get_retry_delay(attempt, response)
                    logger.warning("Rate limited on %s, retrying in %ds (attempt %d/%d)", endpoint, retry_after, attempt + 1, max_retries)
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return self._validate_response(response.json(), endpoint)
                
            except requests.RequestException as e:
                if attempt < max_retries and "429" in str(e):
                    retry_after = self._get_retry_delay(attempt)
                    logger.warning("Rate limited on %s, retrying in %ds (attempt %d/%d)", endpoint, retry_after, attempt + 1, max_retries)
                    time.sleep(retry_after)
                    continue
                logger.exception("Request to Slack API failed on %s", endpoint)
                raise ValueError(f"Failed to communicate with Slack: {e}") from e
        
        raise ValueError(f"Failed to communicate with Slack after {max_retries} retries: rate limited on {endpoint}")
    
    def send_message(self, channel: str, text: str, thread_ts: Optional[str] = None, 
                     blocks: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """Send a message to a Slack channel."""
        data = {"channel": channel, "text": text}
        if thread_ts:
            data["thread_ts"] = thread_ts
        if blocks:
            data["blocks"] = blocks
        result = self._make_request("POST", "chat.postMessage", data)
        return result
    
    def update_message(self, channel: str, ts: str, text: str, blocks: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """Update an existing message in a Slack channel."""
        data = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            data["blocks"] = blocks
        return self._make_request("POST", "chat.update", data)

    def delete_message(self, channel: str, ts: str) -> None:
        """Delete a message from a Slack channel. Raises ValueError on failure."""
        self._make_request("POST", "chat.delete", {"channel": channel, "ts": ts})
    
    def set_channel_topic(self, channel: str, topic: str) -> Dict[str, Any]:
        """Set channel topic/description."""
        return self._make_request("POST", "conversations.setTopic", {"channel": channel, "topic": topic})
    
    def list_bot_channels(self, types: str = "public_channel,private_channel") -> List[Dict[str, Any]]:
        """List channels the bot is a member of (much smaller set than all visible channels)."""
        all_channels = []
        cursor = None
        
        while True:
            data = {"types": types, "exclude_archived": True, "limit": 200}
            if cursor:
                data["cursor"] = cursor
            
            result = self._make_request("GET", "users.conversations", data)
            channels = result.get('channels', [])
            all_channels.extend(channels)
            
            cursor = result.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
        return all_channels
    
    def create_channel(self, name: str, is_private: bool = False) -> Dict[str, Any]:
        """Create a new channel."""
        result = self._make_request("POST", "conversations.create", {"name": name, "is_private": is_private})
        channel = result.get('channel', {})
        return channel
    
    def invite_to_channel(self, channel: str, users: List[str]) -> Optional[Dict[str, Any]]:
        """Add users to a channel automatically (no acceptance required). Returns None on failure."""
        try:
            users_str = ",".join(users) if isinstance(users, list) else users
            return self._make_request("POST", "conversations.invite", {"channel": channel, "users": users_str})
        except Exception:
            logger.warning("Could not add users to channel", exc_info=True)
            return None
    
    def join_channel(self, channel: str) -> Optional[Dict[str, Any]]:
        """Join a public channel by ID. Returns channel info or None on failure."""
        try:
            result = self._make_request("POST", "conversations.join", {"channel": channel})
            return result.get('channel')
        except Exception:
            logger.warning("Could not join channel via conversations.join", exc_info=True)
            return None
    


def _try_create_channel(client: SlackClient, name: str) -> Optional[Dict[str, Any]]:
    """Attempt to create a channel. Returns channel dict on success, None if name is taken."""
    try:
        return client.create_channel(name, is_private=False)
    except ValueError as e:
        if "name_taken" in str(e).lower():
            return None
        raise


def join_existing_incidents_channel(access_token: str, channel_id: str) -> Dict[str, Any]:
    """
    Rejoin a previously-stored incidents channel on reconnect.
    Returns ok=True with channel info if the bot can access the channel,
    or ok=False if the channel no longer exists or is inaccessible.
    """
    try:
        client = SlackClient(access_token)
        channel = client.join_channel(channel_id)
        if channel:
            channel_name = channel.get('name', 'unknown')
            logger.info("Rejoined existing incidents channel on reconnect")
            return {"ok": True, "channel_id": channel_id, "channel_name": channel_name, "created": False}

        logger.info("Could not rejoin stored channel, will create a new one")
        return {"ok": False}
    except Exception:
        logger.warning("Failed to rejoin stored incidents channel", exc_info=True)
        return {"ok": False}


def create_incidents_channel(access_token: str, team_name: str, installer_user_id: str) -> Dict[str, Any]:
    """
    Create an incidents channel for Aurora notifications.
    
    Tries channel names in order until one succeeds:
    1. 'incidents'
    2. 'aurora_incidents'
    3. 'aurora_incidents_<random_suffix>'
    """
    import secrets

    try:
        client = SlackClient(access_token)
        
        candidates = [
            "incidents",
            "aurora_incidents",
            f"aurora_incidents_{secrets.token_hex(4)}",
        ]
        
        channel = None
        channel_name = None
        for name in candidates:
            channel = _try_create_channel(client, name)
            if channel:
                channel_name = name
                break
        
        if not channel or not channel_name:
            logger.error("Failed to create any incidents channel variant")
            return {"ok": False, "error": "Could not create an incidents channel"}
        
        channel_id = channel['id']
        logger.info("Incidents channel created successfully")
        
        try:
            client.invite_to_channel(channel_id, [installer_user_id])
            client.set_channel_topic(channel_id, "Aurora incident alerts notifications")
            client.send_message(channel_id, (
                f"Welcome to #{channel_name}!\n\n"
                f"Aurora is now connected to {team_name}. This channel will be used for:\n\n"
                "• Real-time incident alerts and notifications\n"
                "• Automated root cause analysis updates\n\n"
                "Tag @Aurora in any channel to start a conversation!"
            ))
        except Exception:
            logger.warning("Non-critical error during channel setup", exc_info=True)
        
        return {
            "ok": True,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "created": True,
            "message": f"Created channel #{channel_name}",
        }

    except Exception:
        logger.exception("Failed to create incidents channel")
        return {"ok": False, "error": "Could not create an incidents channel"}


def get_slack_client_for_user(user_id: str) -> Optional[SlackClient]:
    """
    Get authenticated Slack client for a user.
    Shared helper used by routes and notification services.
    """
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        
        slack_creds = get_credentials_from_db(user_id, "slack")
        if not slack_creds or not slack_creds.get("access_token"):
            logger.debug("No Slack credentials for user %s", user_id)
            return None
        
        return SlackClient(slack_creds["access_token"])
    except Exception:
        logger.exception("Failed to get Slack client")
        return None

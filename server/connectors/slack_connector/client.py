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
            logger.error(f"Slack API error on {endpoint}: {error}")
        raise ValueError(f"Slack API error: {error}")

    _REDACTED_ENDPOINTS = frozenset({"chat.postMessage", "chat.update"})

    def _safe_data_repr(self, endpoint: str, data: Optional[Dict]) -> str:
        """Return a log-safe representation of request data, redacting message content."""
        if data is None:
            return "None"
        if endpoint in self._REDACTED_ENDPOINTS:
            return str({k: (v if k == "channel" else "<redacted>") for k, v in data.items()})
        return str(data)

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None, timeout: int = 30, max_retries: int = 3) -> Dict[str, Any]:
        """Make a request to Slack API with retry on 429 rate limits."""
        url = f"{SLACK_API_BASE}/{endpoint}"
        
        for attempt in range(max_retries + 1):
            try:
                t0 = time.time()
                logger.info(f"[SLACK-DEBUG] >>> {method} {endpoint} data={self._safe_data_repr(endpoint, data)} (attempt {attempt})")
                if method == "GET":
                    response = requests.get(url, headers=self.headers, params=data, timeout=timeout)
                else:
                    response = requests.post(url, headers=self.headers, json=data, timeout=timeout)
                
                elapsed = time.time() - t0
                logger.info(f"[SLACK-DEBUG] <<< {method} {endpoint} status={response.status_code} elapsed={elapsed:.2f}s")
                
                if response.status_code == 429 and attempt < max_retries:
                    retry_after = self._get_retry_delay(attempt, response)
                    logger.warning(f"[SLACK-DEBUG] 429 Rate limited on {endpoint}, Retry-After={response.headers.get('Retry-After')}, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                result = response.json()
                logger.info(f"[SLACK-DEBUG] response ok={result.get('ok')} error={result.get('error', 'none')}")
                return self._validate_response(result, endpoint)
                
            except requests.RequestException as e:
                elapsed = time.time() - t0
                logger.warning(f"[SLACK-DEBUG] !!! {method} {endpoint} RequestException after {elapsed:.2f}s: {e}")
                if attempt < max_retries and "429" in str(e):
                    retry_after = self._get_retry_delay(attempt)
                    logger.warning(f"[SLACK-DEBUG] 429 in exception, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_after)
                    continue
                logger.exception("Request to Slack API failed")
                raise ValueError(f"Failed to communicate with Slack: {e}") from e
        
        raise ValueError(f"Failed to communicate with Slack after {max_retries} retries: rate limited on {endpoint}")
    
    def send_message(self, channel: str, text: str, thread_ts: Optional[str] = None, 
                     blocks: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """Send a message to a Slack channel."""
        data = {"channel": channel, "text": text}
        if thread_ts:
            data["thread_ts"] = thread_ts
        if blocks: #Blocks are Slack Block Kit for rich formatting
            data["blocks"] = blocks
        result = self._make_request("POST", "chat.postMessage", data)
        logger.info(f"Message sent to {channel}: {result.get('ts')}")
        return result
    
    def update_message(self, channel: str, ts: str, text: str, blocks: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """Update an existing message in a Slack channel."""
        data = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            data["blocks"] = blocks
        return self._make_request("POST", "chat.update", data)
    
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
        logger.info(f"Created channel: {name} ({channel.get('id')})")
        return channel
    
    def invite_to_channel(self, channel: str, users: List[str]) -> Optional[Dict[str, Any]]:
        """Add users to a channel automatically (no acceptance required). Returns None on failure."""
        try:
            users_str = ",".join(users) if isinstance(users, list) else users
            result = self._make_request("POST", "conversations.invite", {"channel": channel, "users": users_str})
            logger.info(f"Added {len(users) if isinstance(users, list) else 1} user(s) to {channel}")
            return result
        except Exception as e:
            logger.warning(f"Could not add users to channel {channel}: {e}")
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
    logger.info(f"[SLACK-DEBUG] Trying to create channel: #{name}")
    try:
        result = client.create_channel(name, is_private=False)
        logger.info(f"[SLACK-DEBUG] Successfully created #{name}")
        return result
    except ValueError as e:
        if "name_taken" in str(e).lower():
            logger.info(f"[SLACK-DEBUG] #{name} already exists (name_taken), trying next")
            return None
        logger.error(f"[SLACK-DEBUG] Unexpected error creating #{name}: {e}")
        raise


def join_existing_incidents_channel(access_token: str, channel_id: str) -> Dict[str, Any]:
    """
    Rejoin a previously-stored incidents channel on reconnect.
    Returns ok=True with channel info if the bot can access the channel,
    or ok=False if the channel no longer exists or is inaccessible.
    """
    logger.info(f"[SLACK-DEBUG] Attempting to rejoin stored channel: {channel_id}")
    try:
        client = SlackClient(access_token)
        channel = client.join_channel(channel_id)
        if channel:
            channel_name = channel.get('name', 'unknown')
            logger.info(f"[SLACK-DEBUG] Rejoined #{channel_name} ({channel_id})")
            return {"ok": True, "channel_id": channel_id, "channel_name": channel_name, "created": False}

        logger.info(f"[SLACK-DEBUG] Could not rejoin {channel_id}, will create a new one")
        return {"ok": False}
    except Exception as e:
        logger.warning(f"[SLACK-DEBUG] Exception rejoining {channel_id}: {e}", exc_info=True)
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

    logger.info(f"[SLACK-DEBUG] === create_incidents_channel START for team={team_name}, installer={installer_user_id}")
    try:
        client = SlackClient(access_token)
        
        candidates = [
            "incidents",
            "aurora_incidents",
            f"aurora_incidents_{secrets.token_hex(4)}",
        ]
        logger.info(f"[SLACK-DEBUG] Channel candidates: {candidates}")
        
        channel = None
        channel_name = None
        for name in candidates:
            channel = _try_create_channel(client, name)
            if channel:
                channel_name = name
                break
        
        if not channel or not channel_name:
            logger.error(f"[SLACK-DEBUG] [{team_name}] All candidates failed")
            return {"ok": False, "error": "Could not create an incidents channel"}
        
        channel_id = channel['id']
        logger.info(f"[{team_name}] Created incidents channel: #{channel_name} ({channel_id})")
        
        try:
            logger.info(f"[SLACK-DEBUG] Inviting installer {installer_user_id} to {channel_id}")
            client.invite_to_channel(channel_id, [installer_user_id])
            logger.info(f"[SLACK-DEBUG] Setting topic on {channel_id}")
            client.set_channel_topic(channel_id, "Aurora incident alerts notifications")
            logger.info(f"[SLACK-DEBUG] Sending welcome message to {channel_id}")
            client.send_message(channel_id, (
                f"Welcome to #{channel_name}!\n\n"
                f"Aurora is now connected to {team_name}. This channel will be used for:\n\n"
                "• Real-time incident alerts and notifications\n"
                "• Automated root cause analysis updates\n\n"
                "Tag @Aurora in any channel to start a conversation!"
            ))
            logger.info(f"[SLACK-DEBUG] Channel setup complete")
        except Exception as setup_e:
            logger.warning(f"[SLACK-DEBUG] [{team_name}] Non-critical error during channel setup: {setup_e}")
        
        logger.info(f"[SLACK-DEBUG] === create_incidents_channel DONE for team={team_name}")
        return {
            "ok": True,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "created": True,
            "message": f"Created channel #{channel_name}",
        }

    except Exception as e:
        logger.exception(f"[{team_name}] Failed to create incidents channel")
        return {"ok": False, "error": str(e)}


def get_slack_client_for_user(user_id: str) -> Optional[SlackClient]:
    """
    Get authenticated Slack client for a user.
    Shared helper used by routes and notification services.
    """
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        
        slack_creds = get_credentials_from_db(user_id, "slack")
        if not slack_creds or not slack_creds.get("access_token"):
            logger.debug(f"No Slack credentials for user {user_id}")
            return None
        
        return SlackClient(slack_creds["access_token"])
    except Exception as e:
        logger.error(f"Failed to get Slack client for user {user_id}: {e}", exc_info=True)
        return None


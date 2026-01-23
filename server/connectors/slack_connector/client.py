"""
Slack API client for sending and reading messages.
Uses the stored access_token from OAuth to interact with Slack workspace.
"""

import logging
import requests
from typing import Dict, Any, List, Optional
import traceback

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
    
    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None, timeout: int = 30) -> Dict[str, Any]:
        """Make a request to Slack API."""
        url = f"{SLACK_API_BASE}/{endpoint}"
        
        try:
            if method == "GET":
                response = requests.get(url, headers=self.headers, params=data, timeout=timeout)
            else:
                response = requests.post(url, headers=self.headers, json=data, timeout=timeout)
            
            response.raise_for_status()
            result = response.json()
            
            if not result.get('ok', False):
                error = result.get('error', 'unknown_error')
                # Expected errors are handled gracefully
                if error == 'name_taken' and endpoint == 'conversations.create':
                    pass
                else:
                    logger.error(f"Slack API error on {endpoint}: {error}")
                raise ValueError(f"Slack API error: {error}")
            
            return result
            
        except requests.RequestException as e:
            logger.error(f"Request to Slack API failed: {e}")
            raise ValueError(f"Failed to communicate with Slack: {str(e)}")
    
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
    
    def list_channels(self, types: str = "public_channel,private_channel") -> List[Dict[str, Any]]:
        """List channels the bot can see (may include channels the bot is not a member of)."""
        all_channels = []
        cursor = None  # Start with no cursor (first page)
        
        # Extract all channels by calling the API repeatedly until all channels are extracted through slack pagination
        while True:
            data = {"types": types, "exclude_archived": True, "limit": 200}
            if cursor:  # On subsequent iterations, cursor will have a value from previous response
                data["cursor"] = cursor
            
            result = self._make_request("GET", "conversations.list", data)
            channels = result.get('channels', [])
            all_channels.extend(channels)
            
            # Update cursor for next iteration (will be None if no more pages)
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
    
    def set_channel_topic(self, channel: str, topic: str) -> Dict[str, Any]:
        """Set channel topic/description."""
        return self._make_request("POST", "conversations.setTopic", {"channel": channel, "topic": topic})
    
    def find_channel_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a channel by name from channels the bot can see (may not be a member)."""
        for channel in self.list_channels():
            if channel.get('name') == name:
                return channel
        return None


def create_incidents_channel(access_token: str, team_name: str, installer_user_id: str) -> Dict[str, Any]:
    """
    Find or create the incidents channel for Aurora notifications.
    
    Logic:
    1. Try to create 'incidents'
    2. If name_taken, check if Aurora is already a member
    3. If member of 'incidents', use it
    4. If not member of 'incidents', try to create 'aurora_incidents'
    5. If 'aurora_incidents' name_taken, join it
    """
    try:
        client = SlackClient(access_token)
        channel_name = "incidents"
        channel_id = None
        created = False
        message = ""
        
        # 1. Try to create 'incidents'
        try:
            channel = client.create_channel(channel_name, is_private=False)
            channel_id = channel['id']
            created = True
            message = f"Created channel #{channel_name}"
        except ValueError as e:
            if "name_taken" not in str(e).lower():
                raise
            
            # 2 & 3. If name_taken, check membership
            all_channels = client.list_channels()
            channel_map = {ch.get('name'): ch for ch in all_channels}
            incidents_ch = channel_map.get("incidents")
            
            if incidents_ch and incidents_ch.get("is_member"):
                channel_id = incidents_ch['id']
                message = f"Using existing channel #{channel_name}"
            else:
                # 4. Not member of incidents, try 'aurora_incidents'
                channel_name = "aurora_incidents"
                try:
                    channel = client.create_channel(channel_name, is_private=False)
                    channel_id = channel['id']
                    created = True
                    message = f"Created channel #{channel_name}"
                except ValueError as e2:
                    if "name_taken" not in str(e2).lower():
                        raise
                    
                    # 5. Join 'aurora_incidents'
                    aurora_ch = channel_map.get("aurora_incidents")
                    if aurora_ch:
                        channel_id = aurora_ch['id']
                        # 6. Try to join (catch error if already in)
                        try:
                            client._make_request("POST", "conversations.join", {"channel": channel_id})
                            message = f"Joined existing channel #{channel_name}"
                        except Exception:
                            message = f"Using existing channel #{channel_name}"
                    else:
                        raise Exception("Channel #aurora_incidents exists but could not be found")
        
        # 7. Final setup
        if channel_id:
            try:
                client.invite_to_channel(channel_id, [installer_user_id])
                if created:
                    client.set_channel_topic(channel_id, "Aurora incident alerts notifications")
                    client.send_message(channel_id, (
                        f"Welcome to #{channel_name}!\n\n"
                        f"Aurora is now connected to {team_name}. This channel will be used for:\n\n"
                        "• Real-time incident alerts and notifications\n"
                        "• Automated root cause analysis updates\n\n"
                        "Tag @Aurora in any channel to start a conversation!"
                    ))
            except Exception as setup_e:
                logger.warning(f"Error during channel setup: {setup_e}")
                
            return {
                "ok": True, 
                "channel_id": channel_id, 
                "channel_name": channel_name, 
                "created": created, 
                "message": message
            }
            
        return {"ok": False, "error": "Failed to resolve incidents channel"}

    except Exception as e:
        logger.error(f"Failed to create incidents channel: {e}", exc_info=True)
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


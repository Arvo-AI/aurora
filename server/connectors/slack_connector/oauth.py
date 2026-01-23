import os
import logging
import requests
from typing import Dict, Any
from dotenv import load_dotenv
from utils.flags.feature_flags import is_slack_enabled

load_dotenv()

logger = logging.getLogger(__name__)

# OAuth Configuration (following GCP pattern)
CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")

# Use ngrok URL for development if available, otherwise use backend URL
ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

# For development, prefer ngrok URL if available
if ngrok_url and backend_url.startswith("http://localhost"):
    base_url = ngrok_url
else:
    base_url = backend_url

REDIRECT_URI = f"{base_url}/slack/callback"

if is_slack_enabled() and (not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI):
    raise ValueError("Slack OAuth credentials not configured. Please set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, and SLACK_REDIRECT_URI environment variables.")

# Required OAuth scopes for Aurora Slack integration
SLACK_SCOPES = [
    "app_mentions:read",    # Listen for @Aurora mentions in channels
    "chat:write",           # Send messages
    "channels:join",        # Join public channels (required to join aurora_incidents)
    "channels:manage",      # Create channels, invite users, set topics
    "channels:read",        # List public channels
    "channels:history",     # Read public channel history
    "groups:read",          # List private channels
    "groups:history",       # Read private channel history
    "groups:write",         # Create private channels (optional)
    "im:write",             # Send direct messages
    "im:history",           # Read direct message history
    "mpim:write",           # Send group direct messages
    "mpim:history",         # Read group direct message history
    "users:read",           # Read user info
    "users:read.email",     # Read user email addresses
]

def get_auth_url(state: str) -> str:
    """
    Generate the Slack OAuth authorization URL with state parameter.
    """
    if not state:
        raise ValueError("State parameter is required for Slack OAuth. It is used to identify the user in the callback.")
    
    # Build scope string
    scope_string = ",".join(SLACK_SCOPES)
    
    # Build authorization URL (following GCP pattern)
    auth_url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={scope_string}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
    )
    
    return auth_url


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange authorization code for access token."""
    # Exchange code for token
    data = {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }
    response = requests.post("https://slack.com/api/oauth.v2.access", data=data)
    
    # Raise exception if HTTP request failed
    response.raise_for_status()
    token_data = response.json()
    
    # Check for Slack API errors (Slack returns 200 even on errors, checks 'ok' field)
    if not token_data.get('ok', False):
        error = token_data.get('error', 'unknown_error')
        logger.error(f"Slack OAuth token exchange failed: {error}")
        raise ValueError(f"Slack OAuth failed: {error}")
    
    logger.info(f"Successfully exchanged Slack OAuth code for token. Team: {token_data.get('team', {}).get('name', 'unknown')}")
    
    return token_data

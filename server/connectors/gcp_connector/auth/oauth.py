"""
OAuth authentication flow for user's own GCP credentials.
Handles the OAuth2 flow to authenticate users with their Google Cloud Platform accounts.
"""

import os
import requests
import logging
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# OAuth Configuration
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
if not backend_url:
    logger.warning("NEXT_PUBLIC_BACKEND_URL not set - GCP OAuth callbacks will not work")
REDIRECT_URI = f"{backend_url}/callback"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"

# Required OAuth scopes
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
OPENID_SCOPE = "openid"


def get_auth_url(scopes=None, state=None, force_refresh=False):
    """Generate the Google OAuth2 authorization URL with optional state parameter.
    
    Args:
        scopes: Additional OAuth scopes to request
        state: State parameter to pass through OAuth flow
        force_refresh: If True, adds parameters to force refresh token generation
    """
    # Always request the three core scopes; allow caller to append more.
    base_scopes = f"{CLOUD_PLATFORM_SCOPE} {USERINFO_EMAIL_SCOPE} {OPENID_SCOPE}"
    additional_scopes = " ".join(scopes) if scopes else ""
    full_scope = f"{base_scopes} {additional_scopes}".strip()

    auth_url = (
        f"{AUTH_URL}?response_type=code&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&scope={full_scope}"
        f"&access_type=offline&prompt=consent"  # Always request offline access and consent
    )

    # Append state parameter if provided
    if state:
        auth_url += f"&state={state}"

    return auth_url


def exchange_code_for_token(code):
    """Exchange authorization code for access and refresh tokens, and fetch user email."""
    data = {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    response = requests.post(TOKEN_URL, data=data)
    response.raise_for_status()
    token_data = response.json()
    
    # Fetch user email using the access token
    try:
        access_token = token_data.get('access_token')
        if access_token:
            userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"
            headers = {"Authorization": f"Bearer {access_token}"}
            userinfo_response = requests.get(userinfo_url, headers=headers)
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
            
            # Add email to token_data
            token_data['email'] = userinfo.get('email')
            logger.info(f"Successfully fetched user email: {userinfo.get('email')}")
    except Exception as e:
        logger.warning(f"Failed to fetch user email: {e}. Email will not be stored.")
        # Don't fail the whole auth flow if email fetch fails
    
    return token_data


def get_credentials(token_data=None):
    """Retrieve or refresh Google OAuth2 credentials."""
    if not token_data:
        # Try to get token data from database using request context
        try:
            from flask import request, has_request_context
            from utils.auth.stateless_auth import get_user_id_from_request
            from utils.auth.token_management import get_token_data
            
            # Only try to get from request context if we're in a request context
            if has_request_context():
                user_id = get_user_id_from_request()
                if user_id:
                    logger.info(f"No token_data provided, attempting to fetch from database for user: {user_id}")
                    token_data = get_token_data(user_id, "gcp")
                    if token_data:
                        logger.info("Successfully retrieved token data from database")
                    else:
                        logger.warning(f"No GCP token data found in database for user: {user_id}")
                        raise ValueError("No GCP credentials found in database. Please authenticate first.")
                else:
                    logger.warning("No user_id found in request context")
                    raise ValueError("No user_id provided and no session-based fallback available. Please provide token_data or user_id.")
            else:
                # Not in request context (e.g., background tasks)
                logger.error("No token_data provided and not in request context")
                raise ValueError("No token data provided and no request context available. Please provide token_data.")
        except ImportError:
            # stateless_auth not available, fall back to error
            logger.error("stateless_auth module not available")
            raise ValueError("No token data provided and no stateless auth available. Please provide token_data.")
    
    if not token_data:
        raise ValueError("No token data available for authentication.")
    
    try:
        credentials = Credentials(
            token=token_data.get('access_token'),
            refresh_token=token_data.get('refresh_token'),
            token_uri=TOKEN_URL,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=[CLOUD_PLATFORM_SCOPE, USERINFO_EMAIL_SCOPE, OPENID_SCOPE],
        )
        
        # Check if token is expired or about to expire
        if credentials.expired or credentials.token is None:
            if not credentials.refresh_token:
                logger.error("No refresh token available. Re-authentication required.")
                raise ValueError("No refresh token available. Please re-authenticate.")
            
            try:
                credentials.refresh(Request())
                # Update the stored token data with the new token
                token_data['access_token'] = credentials.token
                
                # Store the updated token data if we have user_id
                user_id = token_data.get('user_id')
                if not user_id:
                    # Try to get user_id from request context
                    try:
                        from flask import has_request_context
                        from utils.auth.stateless_auth import get_user_id_from_request
                        if has_request_context():
                            user_id = get_user_id_from_request()
                    except:
                        pass
                
                if user_id:
                    from utils.auth.token_management import store_tokens_in_db
                    store_tokens_in_db(user_id, token_data, "gcp")
                    logger.info("Successfully refreshed and stored access token")
                else:
                    logger.warning("Token refreshed but no user_id available to store updated token")
                    
            except Exception as e:
                logger.error(f"Failed to refresh token: {str(e)}")
                raise ValueError(f"Token refresh failed: {str(e)}")
        
        return credentials
    except Exception as e:
        logger.error(f"Error in get_credentials: {str(e)}")
        raise


def refresh_token_if_needed(token_data):
    """
    Refresh an OAuth token if it's expired or about to expire.
    
    Args:
        token_data: Dict containing 'refresh_token', 'access_token', and 'expires_at'
        
    Returns:
        (bool, dict): (success, new_token_data)
    """
    import time
    
    try:
        refresh_token = token_data.get('refresh_token')
        if not refresh_token:
            logger.error("No refresh token available")
            return False, None
            
        # Check if token is expired or about to expire (within 5 minutes)
        expires_at = token_data.get('expires_at', 0)
        current_time = int(time.time())
        
        # If the token is still valid for more than 5 minutes, return existing token
        if expires_at > current_time + 300:
            return True, token_data
            
        # Token expired or about to expire, refresh it
        logger.info("Token expired or about to expire, refreshing...")
        
        refresh_data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        
        response = requests.post(TOKEN_URL, data=refresh_data)
        if response.status_code != 200:
            logger.error(f"Token refresh failed with status {response.status_code}: {response.text}")
            return False, None
            
        # Parse response
        new_token_data = response.json()
        
        # Merge with original data (keeping the refresh token)
        updated_token_data = {
            "access_token": new_token_data.get("access_token"),
            "token_type": new_token_data.get("token_type", "Bearer"),
            "expires_at": current_time + new_token_data.get("expires_in", 3600),
            "refresh_token": refresh_token  # Keep the original refresh token
        }
        
        logger.info("Token refreshed successfully")
        return True, updated_token_data
        
    except Exception as e:
        logger.error(f"Error refreshing token: {str(e)}")
        return False, None


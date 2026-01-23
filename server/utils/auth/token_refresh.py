"""Token refresh utilities for various providers."""
import logging
import time
import requests
import boto3
import os
from utils.auth.token_management import get_token_data, store_tokens_in_db

# GCP OAuth2 constants
TOKEN_URL = "https://oauth2.googleapis.com/token"
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")


def refresh_token_if_needed(user_id, provider):
    """Refresh the access token if it's expired or about to expire."""
    try:
        token_data = get_token_data(user_id, provider)
        if not token_data:
            logging.warning(f"No token data for user {user_id} and provider {provider}, skipping refresh.")
            return None

        current_time = int(time.time())

        if provider == "aws":
            # Simple static-key flow only
            access_key = token_data.get("aws_access_key_id")
            secret_key = token_data.get("aws_secret_access_key")

            if not access_key or not secret_key:
                logging.error(f"AWS credentials are missing for user {user_id}")
                return None

            try:
                session_obj = boto3.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )
                sts_client = session_obj.client("sts")
                sts_client.get_caller_identity()
                logging.info("AWS static credentials validated via STS")
                return token_data
            except Exception as e:
                logging.error(f"Invalid AWS credentials for user {user_id}: {e}")
                return None

        # GCP and Azure: handle refresh token logic
        expires_at = token_data.get("expires_at", 0)
        if current_time >= expires_at - 300:  # 5 minutes before expiry
            refresh_token = token_data.get("refresh_token")
            if not refresh_token:
                logging.error(f"No refresh token available for {provider}")
                return None

            refresh_data = {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }

            response = requests.post(TOKEN_URL, data=refresh_data)
            if response.status_code != 200:
                logging.error(f"Token refresh failed for {provider}: {response.text}")
                return None

            new_token_data = response.json()
            token_data['access_token'] = new_token_data['access_token']
            token_data['expires_at'] = current_time + new_token_data['expires_in']
            token_data['refresh_token'] = token_data.get('refresh_token')  # Keep the existing one

            store_tokens_in_db(user_id, token_data, provider)
            logging.info(f"{provider.upper()} token refreshed successfully")

        return token_data

    except Exception as e:
        logging.error(f"Error refreshing token for {provider}: {e}")
        return None

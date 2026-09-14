import logging
import requests

# NOTE: This file now uses USER credentials, not hardcoded service principal credentials
# All Azure operations should use the user's own service principal via auth_tools.py

# The functions in this file expect management tokens generated from user credentials
# via generate_azure_access_token(user_id) from utils.auth.cloud_auth.py


logging.info("Azure billing connector initialized - uses user-specific credentials via auth_tools")

def fetch_subscriptions(management_token):
    """Fetch all subscriptions accessible to the service principal (ID and name)."""
    try:
        headers = {
            "Authorization": f"Bearer {management_token}",
            "Content-Type": "application/json"
        }
        # ARM paginates this endpoint via nextLink. Following it matters for tenants
        # with many subscriptions: callers treat this list as the complete set of
        # accessible subscriptions, so a truncated first page would silently drop
        # subscriptions from fan-out and mark them inactive on the next login.
        url = "https://management.azure.com/subscriptions?api-version=2020-01-01"
        subscriptions = []
        for _ in range(50):  # bound the walk; 50 pages is far beyond any real tenant
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            subscriptions.extend(payload.get("value", []))
            url = payload.get("nextLink")
            if not url:
                break
        else:
            logging.warning("Subscription listing hit the page cap; list may be partial")
        # Return list of dicts with subscriptionId and displayName
        return [
            {
                "subscriptionId": sub["subscriptionId"],
                "displayName": sub.get("displayName", "Unnamed Subscription"),
                "state": sub.get("state")
            }
            for sub in subscriptions
        ]
    except Exception as e:
        logging.error(f"Error fetching subscriptions: {e}")
        return []

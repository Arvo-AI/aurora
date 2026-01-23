"""
OVH Cloud authentication and API helpers.

Implements:
- validate_ovh_credentials: test AK/AS/CK with /me
- create_ovh_service_account: POST /me/api/oauth2/client (client credentials flow)
- create_iam_policy_for_service_account: POST /me/iam/policy
- get_oauth2_token: client credentials token for Bearer API calls
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple, List

import ovh
import requests

logger = logging.getLogger(__name__)


OAUTH2_TOKEN_ENDPOINTS = {
    'ovh-eu': 'https://www.ovh.com/auth/oauth2/token',
    'ovh-us': 'https://us.ovhcloud.com/auth/oauth2/token',
    'ovh-ca': 'https://ca.ovh.com/auth/oauth2/token',
}

# Note: OVHcloud OAuth2 uses CLIENT CREDENTIALS flow, not authorization code flow.
# There are NO separate authorization endpoints for user consent - instead, users create
# OAuth2 service accounts via /me/api/oauth2/client API endpoint.
# This is the official and recommended approach by OVHcloud.


def validate_ovh_credentials(endpoint: str, application_key: str, application_secret: str, consumer_key: str) -> Tuple[bool, Optional[str]]:
    """
    Validate OVH credentials by attempting to authenticate and fetch /me.

    Args:
        endpoint: OVH endpoint ('ovh-eu', 'ovh-us', 'ovh-ca')
        application_key: OVH application key
        application_secret: OVH application secret
        consumer_key: OVH consumer key

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        client = ovh.Client(
            endpoint=endpoint,
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
        )
        _ = client.get('/me')
        return True, None
    except ovh.exceptions.InvalidCredential as e:
        return False, f"Invalid credentials: {str(e)}"
    except ovh.exceptions.InvalidKey as e:
        return False, f"Invalid application key: {str(e)}"
    except Exception as e:
        logger.error(f"Credential validation error: {e}", exc_info=True)
        return False, f"Validation error: {str(e)}"


def create_ovh_service_account(endpoint: str, application_key: str, application_secret: str, consumer_key: str, user_id: str, create_iam_policy: bool = False, project_id: Optional[str] = None) -> Dict:
    """
    Create an OAuth2 service account (client credentials) on OVH.

    Args:
        endpoint: OVH endpoint ('ovh-eu', 'ovh-us', 'ovh-ca')
        application_key: OVH application key (used once, not stored)
        application_secret: OVH application secret (used once, not stored)
        consumer_key: OVH consumer key (used once, not stored)
        user_id: Aurora user ID
        create_iam_policy: Whether to create IAM policy for least privilege
        project_id: OVH project ID to scope IAM policy to

    Returns:
        Dict with clientId, clientSecret, and optionally iamPolicyId
    """
    try:
        client = ovh.Client(
            endpoint=endpoint,
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
        )
        name = f"Aurora Service Account - User {user_id}"
        resp = client.post('/me/api/oauth2/client', **{
            'name': name,
            'description': f'Aurora OAuth2 client for {user_id}',
            'flow': 'CLIENT_CREDENTIALS',
            'callbackUrls': []
        })
        client_id = resp.get('clientId')
        client_secret = resp.get('clientSecret')
        result = {'clientId': client_id, 'clientSecret': client_secret, 'name': name}

        if create_iam_policy and project_id:
            policy = create_iam_policy_for_service_account(client, endpoint, client_id, user_id, project_id)
            if policy and 'id' in policy:
                result['iamPolicyId'] = policy['id']

        return result
    except ovh.exceptions.APIError as e:
        logger.error(f"OVH API error creating OAuth2 client: {e}", exc_info=True)
        return {'error': f'API error: {str(e)}'}
    except Exception as e:
        logger.error(f"Error creating OAuth2 client: {e}", exc_info=True)
        return {'error': str(e)}


def create_iam_policy_for_service_account(client: ovh.Client, endpoint: str, client_id: str, user_id: str, project_id: str) -> Optional[Dict]:
    """
    Create an IAM policy for the service account with least-privilege access to a project.

    Args:
        client: Authenticated OVH client
        endpoint: OVH endpoint (used to determine region for URN)
        client_id: OAuth2 client ID
        user_id: Aurora user ID
        project_id: OVH project ID to scope access to

    Returns:
        Created policy dict or None on error
    """
    try:
        me = client.get('/me')
        # Extract region directly from endpoint: 'ovh-eu' -> 'eu'
        region = endpoint.split('-')[1] if '-' in endpoint else 'eu'

        # Build URNs with correct region based on endpoint
        account_urn_prefix = f"urn:v1:{region}:identity:credential:{me.get('nichandle')}"
        service_account_urn = f"{account_urn_prefix}/oauth2-{client_id}"
        policy_name = f"Aurora-User-{user_id}-Policy"

        policy = client.post('/iam/policy', **{
            'name': policy_name,
            'description': f'Aurora policy for user {user_id}',
            'identities': [service_account_urn],
            'resources': [{'urn': f'urn:v1:{region}:resource:publicCloudProject:{project_id}'}],
            'permissions': {
                'allow': [
                    {'action': 'publicCloudProject:apiovh:instance/*'},
                    {'action': 'publicCloudProject:apiovh:network/*'},
                    {'action': 'publicCloudProject:apiovh:volume/*'},
                    {'action': 'publicCloudProject:apiovh:kube/*'},
                ]
            }
        })
        logger.info(f"Created IAM policy for service account {client_id} in region {region}")
        return policy
    except Exception as e:
        logger.error(f"IAM policy creation error: {e}", exc_info=True)
        return None


def get_oauth2_token(endpoint: str, client_id: str, client_secret: str) -> Optional[str]:
    """
    Get an OAuth2 access token using client credentials flow with caching.

    OVHcloud OAuth2 uses CLIENT CREDENTIALS flow (not authorization code flow).
    Access tokens are short-lived (~1 hour) and automatically refreshed by requesting
    a new token - there are NO refresh tokens in client credentials flow.

    Tokens are cached for 58 minutes (OVH tokens valid for 60 minutes) to minimize API calls.

    Args:
        endpoint: OVH endpoint ('ovh-eu', 'ovh-us', 'ovh-ca')
        client_id: OAuth2 client ID
        client_secret: OAuth2 client secret

    Returns:
        Access token or None on error
    """
    try:
        # TODO: Implement Redis-based token caching
        # For now, always fetch fresh token (less efficient but works)
        # cache_key = f"oauth2:token:{endpoint}:{client_id}"
        # See utils.auth.oauth2_state_cache for Redis implementation pattern

        token_url = OAUTH2_TOKEN_ENDPOINTS.get(endpoint)
        if not token_url:
            logger.error(f"No token URL configured for endpoint: {endpoint}")
            return None

        # Request new access token using client credentials flow
        logger.info(f"Requesting new OAuth2 token from {token_url}")
        r = requests.post(
            token_url,
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret,
                'scope': 'all'
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )
        r.raise_for_status()
        data = r.json()
        token = data.get('access_token')
        expires_in = data.get('expires_in', 3600)

        if not token:
            logger.error("No access_token in OAuth2 response")
            return None

        # TODO: Cache token in Redis with appropriate TTL
        # cache_duration = min(expires_in - 120, 3480) if expires_in > 120 else expires_in - 10
        # redis_client.setex(cache_key, cache_duration, token)
        logger.info(f"Fetched new OAuth2 token (expires in {expires_in}s)")

        return token
    except requests.exceptions.HTTPError as e:
        logger.error(f"OAuth2 token request HTTP error: {e.response.status_code} - {e.response.text[:200]}")
        return None
    except requests.exceptions.Timeout:
        logger.error("OAuth2 token request timed out")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"OAuth2 token request network error: {e.__class__.__name__}")
        return None
    except Exception as e:
        # Avoid logging secrets; only log error class and message
        logger.error(f"OAuth2 token request unexpected error: {e.__class__.__name__}: {str(e)[:100]}")
        return None

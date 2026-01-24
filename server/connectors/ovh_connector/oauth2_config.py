"""OVH OAuth2 configuration loader.

Loads OAuth2 credentials using Aurora's standard pattern:
- Production/Staging: HashiCorp Vault
- Development: Environment variables
"""
import os
import logging
from typing import Dict

logger = logging.getLogger(__name__)


def get_oauth2_config() -> Dict[str, Dict[str, str]]:
    """
    Get OAuth2 client configuration for all regions.

    Returns:
        Dict mapping endpoints to OAuth2 configs. Example:
        {
            'ovh-eu': {
                'client_id': '...',
                'client_secret': '...',
                'redirect_uri': '...'
            }
        }

    Configuration pattern (follows Aurora standard):
    - Production/Staging: Credentials from HashiCorp Vault
    - Development: Credentials from environment variables
    - Redirect URIs always from environment (not secrets)

    Redirect URI is constructed from NEXT_PUBLIC_BACKEND_URL (should include /backend in production)
    """
    env = os.environ.get('AURORA_ENV', '').lower()
    config = {}

    for region in ['ovh-eu', 'ovh-us', 'ovh-ca']:
        try:
            client_id = os.getenv(f'{region.upper().replace("-", "_")}_CLIENT_ID')
            client_secret = os.getenv(f'{region.upper().replace("-", "_")}_CLIENT_SECRET')

            # Skip region if not configured
            if not client_id or not client_secret:
                logger.warning(f"OAuth2 credentials not configured for {region}")
                continue

            # Redirect URI: construct from NEXT_PUBLIC_BACKEND_URL (or ngrok URL for development)
            ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
            backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

            # For development, prefer ngrok URL if available
            if ngrok_url and backend_url.startswith("http://localhost"):
                base_url = ngrok_url
            else:
                base_url = backend_url

            if not base_url:
                logger.error(f"Missing redirect URI for {region} (set NEXT_PUBLIC_BACKEND_URL)")
                continue
            redirect_uri = f"{base_url}/ovh_api/ovh/oauth2/callback"

            config[region] = {
                'client_id': client_id,
                'client_secret': client_secret,
                'redirect_uri': redirect_uri,
            }

            logger.info(f"OAuth2 configured for {region}")

        except Exception as e:
            logger.warning(f"Failed to load OAuth2 config for {region}: {e}")
            continue

    return config

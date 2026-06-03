"""
Fly.io API token validation and permission tier detection.

Supports org-scoped tokens:
- Read-only tokens (fly tokens create readonly -o <org>)
- Full org deploy tokens (fly tokens create org -o <org>)
"""

import logging
from typing import Dict, Optional, Tuple

from connectors.flyio_connector.api_client import FlyioClient

logger = logging.getLogger(__name__)


def validate_flyio_token(api_token: str, org_slug: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate a Fly.io API token and detect its permission tier.

    Uses FlyioClient.list_apps() to verify the token works and retrieve the
    app list that gets shown on the frontend. Then probes a write endpoint
    to determine the tier.
    """
    if not api_token or not org_slug:
        return False, None, "API token and organization slug are required"

    api_token = api_token.strip()
    org_slug = org_slug.strip().lower()

    client = FlyioClient(api_token, org_slug)
    apps = client.list_apps()

    if apps is None:
        return False, None, "Failed to connect to Fly.io. Check your token and org slug."

    tier = "full" if client.has_write_access() else "readonly"
    apps_info = [
        {"name": a.get("name", a.get("id", "unknown")), "status": a.get("status", "unknown")}
        for a in apps
    ]

    token_info = {
        "org_slug": org_slug,
        "tier": tier,
        "apps": apps_info,
    }

    return True, token_info, None

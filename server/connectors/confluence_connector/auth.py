"""Authentication helpers for the Confluence connector.

Thin delegation layer over the shared atlassian_auth module, preserving the
public API that existing callers (confluence_routes, runbook_utils, etc.) rely on.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from connectors.atlassian_auth.auth import (
    exchange_code_for_token as _exchange,
    fetch_accessible_resources,  # noqa: F401 – re-exported
    get_auth_url as _get_auth_url,
    refresh_access_token,  # noqa: F401 – re-exported
    select_resource_for_product,
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "")
REDIRECT_URI = f"{FRONTEND_URL}/confluence/callback"


def get_auth_url(state: str) -> str:
    """Generate the Atlassian OAuth 2.0 authorization URL for Confluence."""
    return _get_auth_url(state, products=["confluence"], redirect_uri=REDIRECT_URI)


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange OAuth authorization code for access and refresh tokens."""
    return _exchange(code, redirect_uri=REDIRECT_URI)


def select_confluence_resource(
    resources: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Pick a Confluence resource from accessible resources."""
    return select_resource_for_product(resources, "confluence")

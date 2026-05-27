"""Multi-SA credential loading helpers.

Bridges the new ``user_connections`` storage (one row per uploaded SA key) with
the legacy code paths that still expect a single ``token_data`` dict from
``user_tokens``. Read-only — never writes.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from google.oauth2 import service_account as google_sa

from utils.db.connection_utils import get_all_user_connections, find_connection_for_project
from utils.secrets.secret_ref_utils import get_connection_secret

logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _unwrap_sa_info(sa_info: dict) -> dict:
    """Normalize stored SA blobs to the raw SA dict shape google-auth expects.

    Aurora has historically stored SA credentials in two shapes inside the
    Vault/Secrets-Manager blob:

    1. The raw GCP key JSON (``{type, project_id, client_email, private_key,
       ...}``) — what google-auth wants directly.
    2. A wrapper from the legacy connect route: ``{service_account_json: "<raw
       JSON string>", client_email, project_id}``.

    This helper accepts either shape and returns the raw dict.
    """
    if not isinstance(sa_info, dict):
        return sa_info
    raw = sa_info.get("service_account_json")
    if raw and isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            # Returning the wrapper dict will fail downstream in google-auth
            # with a confusing "missing 'type'" error; log so the real cause
            # is recoverable from logs.
            logger.warning(
                "[GCP-MultiSA] Stored service_account_json is not valid JSON: %s",
                exc,
            )
    elif isinstance(raw, dict):
        return raw
    return sa_info


def _build_sa_credentials(sa_info: dict, scopes: Optional[List[str]] = None):
    """Build a google-auth Credentials object from an SA JSON dict."""
    return google_sa.Credentials.from_service_account_info(
        _unwrap_sa_info(sa_info), scopes=scopes or _DEFAULT_SCOPES
    )


def load_gcp_connections_with_creds(
    user_id: str, scopes: Optional[List[str]] = None
) -> List[Tuple[dict, object]]:
    """Return ``[(connection_row, Credentials), ...]`` for every active SA.

    Empty list when the user has no ``user_connections`` rows for GCP, even if a
    legacy ``user_tokens`` row exists — callers handle the fallback themselves
    so they can keep their existing single-cred logic.
    """
    rows = get_all_user_connections(user_id, "gcp") or []
    out: List[Tuple[dict, object]] = []
    for row in rows:
        ref = row.get("secret_ref")
        if not ref:
            continue
        sa_info = get_connection_secret(ref)
        if not sa_info:
            logger.warning(
                "[GCP-MultiSA] Could not load secret for account=%s — skipping",
                (row.get("account_id") or "")[:12] + "...",
            )
            continue
        try:
            creds = _build_sa_credentials(sa_info, scopes)
        except Exception as e:
            logger.warning(
                "[GCP-MultiSA] Failed to build credentials for account=%s: %s",
                (row.get("account_id") or "")[:12] + "...",
                type(e).__name__,
            )
            continue
        out.append((row, creds))
    return out


def load_sa_json_for_project(user_id: str, project_id: Optional[str]) -> Optional[dict]:
    """Return the raw SA JSON dict that should be used for ``project_id``.

    Falls back to the first active SA when ``project_id`` is None or no SA
    matches. Returns None when the user has no multi-SA connections at all.
    """
    if project_id:
        row = find_connection_for_project(user_id, "gcp", project_id)
        if row and row.get("secret_ref"):
            sa_info = get_connection_secret(row["secret_ref"])
            if sa_info:
                return _unwrap_sa_info(sa_info)
    rows = get_all_user_connections(user_id, "gcp") or []
    for row in rows:
        ref = row.get("secret_ref")
        if not ref:
            continue
        sa_info = get_connection_secret(ref)
        if sa_info:
            return _unwrap_sa_info(sa_info)
    return None


def load_gcp_credentials_for_project(
    user_id: str, project_id: str, scopes: Optional[List[str]] = None
) -> Optional[Tuple[dict, object]]:
    """Resolve a single ``(connection_row, Credentials)`` for ``project_id``.

    Returns None when no SA owns or has access to the project.
    """
    row = find_connection_for_project(user_id, "gcp", project_id)
    if not row or not row.get("secret_ref"):
        return None
    sa_info = get_connection_secret(row["secret_ref"])
    if not sa_info:
        return None
    try:
        return row, _build_sa_credentials(sa_info, scopes)
    except Exception as e:
        logger.warning(
            "[GCP-MultiSA] Failed to build credentials for project=%s: %s",
            project_id,
            type(e).__name__,
        )
        return None

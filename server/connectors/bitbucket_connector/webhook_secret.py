"""Per-org Bitbucket change-gating webhook secret.

One HMAC secret per org, shared by every enrolled repo's Bitbucket hook
(the setup UI tells the admin to paste the SAME secret into each repo).
The secret value lives in the active secrets backend (Vault / AWS SM);
``organizations.bitbucket_webhook_secret_ref`` stores only the reference.

- Created lazily the first time Incident Prevention is enabled for a repo.
- Rotation = delete the ref and re-enable (a new secret is minted); the
  admin must then re-paste it into every repo hook.
- The value is returned to the connector UI exactly once per GET so the
  admin can copy it — it is never logged.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from typing import Optional

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_SECRET_NAME_TEMPLATE = "bitbucket-change-gating-webhook-{org_id}"


def _get_ref(org_id: str) -> Optional[str]:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            # organizations is not RLS-protected (queried pre-auth).
            cur.execute(
                "SELECT bitbucket_webhook_secret_ref FROM organizations WHERE id = %s",
                (org_id,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None


def get_webhook_secret(org_id: str) -> Optional[str]:
    """Return the org's webhook secret value, or None when never created."""
    ref = _get_ref(org_id)
    if not ref:
        return None
    try:
        from utils.secrets.secret_ref_utils import SecretRefManager

        return SecretRefManager().get_secret(ref)
    except Exception as exc:
        logger.error(
            "[BitbucketWebhookSecret] failed to read secret for org %s: %s",
            org_id, type(exc).__name__,
        )
        return None


def get_or_create_webhook_secret(org_id: str) -> str:
    """Return the org's webhook secret, creating it on first use."""
    existing = get_webhook_secret(org_id)
    if existing:
        return existing

    from utils.secrets.secret_ref_utils import SecretRefManager

    value = _secrets.token_hex(32)
    ref = SecretRefManager().store_secret(
        _SECRET_NAME_TEMPLATE.format(org_id=org_id), value
    )
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE organizations
                      SET bitbucket_webhook_secret_ref = %s, updated_at = NOW()
                    WHERE id = %s
                      AND bitbucket_webhook_secret_ref IS NULL""",
                (ref, org_id),
            )
            raced = cur.rowcount == 0
        conn.commit()
    if raced:
        # A concurrent enable won the ref write — use theirs, drop ours.
        winner = get_webhook_secret(org_id)
        if winner:
            try:
                SecretRefManager().delete_secret(ref)
            except Exception:
                logger.warning(
                    "[BitbucketWebhookSecret] failed to clean up losing secret "
                    "for org %s", org_id,
                )
            return winner
    logger.info("[BitbucketWebhookSecret] created webhook secret for org %s", org_id)
    return value


def delete_webhook_secret(org_id: str) -> None:
    """Delete the org's webhook secret (disconnect-all cleanup). Best-effort."""
    ref = _get_ref(org_id)
    if not ref:
        return
    try:
        from utils.secrets.secret_ref_utils import SecretRefManager

        SecretRefManager().delete_secret(ref)
    except Exception as exc:
        logger.warning(
            "[BitbucketWebhookSecret] backend delete failed for org %s: %s",
            org_id, type(exc).__name__,
        )
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE organizations
                      SET bitbucket_webhook_secret_ref = NULL, updated_at = NOW()
                    WHERE id = %s""",
                (org_id,),
            )
        conn.commit()

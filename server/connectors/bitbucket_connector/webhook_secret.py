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
import os
import secrets as _secrets
from typing import Optional

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_SECRET_NAME_TEMPLATE = "bitbucket-change-gating-webhook-{org_id}"


def webhook_base_url() -> str:
    """Externally reachable API base for webhook URLs (ngrok-aware in dev).

    Shared by the Flask enable/verify routes and the Celery dispatcher so
    both agree on the URL a delivery is expected to arrive at. The Flask
    request fallback only applies when there IS a request context — in a
    worker it returns "" rather than raising, and callers skip URL scoping.
    """
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url
    if not base_url:
        try:
            from flask import has_request_context, request

            if has_request_context():
                base_url = request.host_url.rstrip("/")
        except Exception:
            base_url = ""
    return base_url


def webhook_url_for_org(org_id: str) -> str:
    """Full delivery URL for an org's hooks, or "" when the base is unknown."""
    base = webhook_base_url()
    return f"{base}/bitbucket/webhook/{org_id}" if base else ""


class WebhookSecretUnavailable(Exception):
    """The org HAS a secret ref but the secrets backend read failed.

    Distinct from "no secret configured" (``get_webhook_secret`` → None):
    the webhook route answers 503 for this — Bitbucket retries 5xx but
    drops deliveries permanently on 4xx, so a transient Vault/AWS SM
    outage must not read as an authentication failure.
    """


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
    """Return the org's webhook secret value, or None when never created.

    Raises :class:`WebhookSecretUnavailable` when a ref exists but the
    backend read fails — callers must treat that as transient, not as
    "this org has no secret".
    """
    ref = _get_ref(org_id)
    if not ref:
        return None
    try:
        from utils.secrets.secret_ref_utils import SecretRefManager

        return SecretRefManager().get_secret(ref)
    except Exception as exc:
        logger.exception(
            "[BitbucketWebhookSecret] failed to read secret for org %s", org_id
        )
        raise WebhookSecretUnavailable(
            f"secrets backend read failed for org {org_id}"
        ) from exc


def get_or_create_webhook_secret(org_id: str) -> str:
    """Return the org's webhook secret, creating it on first use.

    Raises when the org row is missing or the backend is unavailable —
    the enable endpoint must fail loudly rather than hand the admin a
    secret that no delivery will ever validate against.
    """
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
        # rowcount == 0 has two causes: a concurrent enable already wrote a
        # ref (use theirs), or the organizations row doesn't exist at all
        # (fail loudly — returning the fresh value would hand the admin a
        # secret that no delivery will ever validate against).
        def _drop_losing_secret() -> None:
            try:
                SecretRefManager().delete_secret(ref)
            except Exception:
                logger.warning(
                    "[BitbucketWebhookSecret] failed to clean up losing secret "
                    "for org %s", org_id,
                )

        winner = get_webhook_secret(org_id)
        if winner:
            _drop_losing_secret()
            return winner
        _drop_losing_secret()
        raise RuntimeError(
            f"cannot persist webhook secret ref: no organizations row for {org_id}"
        )
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

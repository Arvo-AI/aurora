"""Clerk webhook handler for user/org lifecycle events in SaaS mode.

When AURORA_SAAS_MODE=true, Clerk manages authentication. Users are created in
Clerk's dashboard and synced to our DB via these webhooks. The Clerk user ID
becomes the canonical user_id used in X-User-ID headers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid as _uuid

from flask import Blueprint, jsonify, request

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

clerk_webhook_bp = Blueprint("clerk_webhook", __name__, url_prefix="/api/webhooks")

CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET", "")


def _verify_clerk_signature(payload: bytes, headers: dict) -> bool:
    """Verify Clerk webhook signature using Svix headers."""
    normalized = {k.lower(): v for k, v in headers.items()}

    svix_id = normalized.get("svix-id")
    svix_timestamp = normalized.get("svix-timestamp")
    svix_signature = normalized.get("svix-signature")

    if not all([svix_id, svix_timestamp, svix_signature, CLERK_WEBHOOK_SECRET]):
        return False

    # Reject replayed events older than 5 minutes
    try:
        ts = int(svix_timestamp)
        if abs(time.time() - ts) > 300:
            logger.warning("[CLERK] Webhook timestamp too old: %s", svix_timestamp)
            return False
    except (ValueError, TypeError):
        return False

    secret = CLERK_WEBHOOK_SECRET
    if secret.startswith("whsec_"):
        secret = secret[6:]

    secret_bytes = base64.b64decode(secret)
    to_sign = f"{svix_id}.{svix_timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = base64.b64encode(
        hmac.HMAC(secret_bytes, to_sign, hashlib.sha256).digest()
    ).decode("utf-8")

    signatures = svix_signature.split(" ")
    for sig in signatures:
        sig_value = sig.split(",", 1)[-1] if "," in sig else sig
        if hmac.compare_digest(expected, sig_value):
            return True

    return False


def _handle_user_created(data: dict):
    """Sync a newly created Clerk user to our database."""
    clerk_user_id = data.get("id")
    email = None
    email_addresses = data.get("email_addresses", [])
    for addr in email_addresses:
        if addr.get("id") == data.get("primary_email_address_id"):
            email = addr.get("email_address")
            break
    if not email and email_addresses:
        email = email_addresses[0].get("email_address")

    first_name = data.get("first_name", "")
    last_name = data.get("last_name", "")
    name = f"{first_name} {last_name}".strip() or email

    if not clerk_user_id or not email:
        logger.warning("[CLERK] user.created missing id or email")
        return

    slug = f"{email.split('@')[0]}-{_uuid.uuid4().hex[:8]}"

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            # Create user first (org FK on created_by requires user to exist)
            cursor.execute(
                """INSERT INTO users (id, email, name, role, password_hash)
                   VALUES (%s, %s, %s, 'admin', 'clerk_managed')
                   ON CONFLICT (id) DO UPDATE SET
                     email = EXCLUDED.email,
                     name = EXCLUDED.name,
                     updated_at = CURRENT_TIMESTAMP""",
                (clerk_user_id, email, name),
            )

            # Create org for the user
            cursor.execute(
                """INSERT INTO organizations (name, slug, created_by)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (slug) DO NOTHING
                   RETURNING id""",
                (f"{name}'s Organization", slug, clerk_user_id),
            )
            org_row = cursor.fetchone()

            if org_row:
                org_id = org_row[0]
            else:
                cursor.execute(
                    "SELECT id FROM organizations WHERE created_by = %s LIMIT 1",
                    (clerk_user_id,),
                )
                existing = cursor.fetchone()
                org_id = existing[0] if existing else None

            # Link user to org
            if org_id:
                cursor.execute(
                    "UPDATE users SET org_id = %s WHERE id = %s",
                    (org_id, clerk_user_id),
                )

            # Create free tier subscription for the org
            if org_id:
                cursor.execute(
                    """INSERT INTO org_subscriptions (org_id, stripe_customer_id, plan_tier, status)
                       VALUES (%s, %s, 'free', 'active')
                       ON CONFLICT (org_id) DO NOTHING""",
                    (org_id, ""),
                )

            conn.commit()

    logger.info("[CLERK] Created user %s (%s) with org %s", clerk_user_id, email, org_id)


def _handle_user_updated(data: dict):
    """Sync updated Clerk user data."""
    clerk_user_id = data.get("id")
    email = None
    email_addresses = data.get("email_addresses", [])
    for addr in email_addresses:
        if addr.get("id") == data.get("primary_email_address_id"):
            email = addr.get("email_address")
            break

    first_name = data.get("first_name", "")
    last_name = data.get("last_name", "")
    name = f"{first_name} {last_name}".strip()

    if not clerk_user_id:
        return

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            updates = []
            params = []
            if email:
                updates.append("email = %s")
                params.append(email)
            if name:
                updates.append("name = %s")
                params.append(name)
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(clerk_user_id)
                cursor.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE id = %s",
                    params,
                )
                conn.commit()

    logger.info("[CLERK] Updated user %s", clerk_user_id)


def _handle_user_deleted(data: dict):
    """Handle Clerk user deletion — soft delete or mark inactive."""
    clerk_user_id = data.get("id")
    if not clerk_user_id:
        return

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET role = 'disabled', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (clerk_user_id,),
            )
            conn.commit()

    logger.info("[CLERK] Disabled user %s", clerk_user_id)


@clerk_webhook_bp.route("/clerk", methods=["POST"])
def handle_clerk_webhook():
    """Process incoming Clerk webhook events."""
    payload = request.get_data()

    if not _verify_clerk_signature(payload, dict(request.headers)):
        logger.warning("[CLERK] Invalid webhook signature")
        return jsonify({"error": "Invalid signature"}), 400

    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = event.get("type", "")
    data = event.get("data", {})

    handlers = {
        "user.created": _handle_user_created,
        "user.updated": _handle_user_updated,
        "user.deleted": _handle_user_deleted,
    }

    handler = handlers.get(event_type)
    if handler:
        try:
            handler(data)
        except Exception:
            logger.exception("[CLERK] Error handling event type=%s", event_type)
            return jsonify({"error": "Processing failed"}), 500
    else:
        logger.debug("[CLERK] Unhandled event type: %s", event_type)

    return jsonify({"status": "ok"})

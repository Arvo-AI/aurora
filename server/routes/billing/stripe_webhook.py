"""Stripe webhook handler for subscription lifecycle events."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import stripe
from flask import Blueprint, jsonify, request

from routes.billing.plans import PlanTier
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

stripe_webhook_bp = Blueprint("stripe_webhook", __name__, url_prefix="/api/webhooks")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

PRICE_TO_TIER = {}


def _build_price_to_tier_map():
    """Build reverse lookup from Stripe price IDs to plan tiers."""
    global PRICE_TO_TIER
    mappings = {
        "STRIPE_PRICE_PRO_MONTHLY": PlanTier.PRO,
        "STRIPE_PRICE_PRO_YEARLY": PlanTier.PRO,
        "STRIPE_PRICE_ENTERPRISE_MONTHLY": PlanTier.ENTERPRISE,
        "STRIPE_PRICE_ENTERPRISE_YEARLY": PlanTier.ENTERPRISE,
    }
    for env_key, tier in mappings.items():
        price_id = os.getenv(env_key, "")
        if price_id:
            PRICE_TO_TIER[price_id] = tier


_build_price_to_tier_map()


def _tier_from_subscription(subscription) -> PlanTier:
    """Determine plan tier from a Stripe subscription object."""
    if subscription.get("items") and subscription["items"].get("data"):
        for item in subscription["items"]["data"]:
            price_id = item.get("price", {}).get("id", "")
            if price_id in PRICE_TO_TIER:
                return PRICE_TO_TIER[price_id]
    return PlanTier.FREE


def _record_event(cursor, conn, event_id: str, event_type: str, org_id: str | None, payload: dict):
    """Record a Stripe event for idempotency and audit."""
    cursor.execute(
        "INSERT INTO stripe_events (stripe_event_id, event_type, org_id, payload) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id",
        (event_id, event_type, org_id, json.dumps(payload)),
    )
    result = cursor.fetchone()
    conn.commit()
    return result is not None  # True if new event, False if duplicate


def _handle_checkout_completed(session: dict):
    """Handle checkout.session.completed — activate subscription."""
    org_id = session.get("metadata", {}).get("org_id")
    subscription_id = session.get("subscription")
    customer_id = session.get("customer")

    if not org_id or not subscription_id:
        logger.warning("[STRIPE] checkout.session.completed missing org_id or subscription_id")
        return

    subscription = stripe.Subscription.retrieve(subscription_id)
    tier = _tier_from_subscription(subscription)

    period_start = datetime.fromtimestamp(subscription.current_period_start, tz=timezone.utc)
    period_end = datetime.fromtimestamp(subscription.current_period_end, tz=timezone.utc)

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO org_subscriptions
                   (org_id, stripe_customer_id, stripe_subscription_id, plan_tier, status,
                    billing_period_start, billing_period_end)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (org_id) DO UPDATE SET
                     stripe_customer_id = EXCLUDED.stripe_customer_id,
                     stripe_subscription_id = EXCLUDED.stripe_subscription_id,
                     plan_tier = EXCLUDED.plan_tier,
                     status = EXCLUDED.status,
                     billing_period_start = EXCLUDED.billing_period_start,
                     billing_period_end = EXCLUDED.billing_period_end,
                     cancel_at_period_end = FALSE,
                     updated_at = CURRENT_TIMESTAMP""",
                (org_id, customer_id, subscription_id, tier.value, "active",
                 period_start, period_end),
            )
            conn.commit()

    logger.info("[STRIPE] Activated %s subscription for org %s", tier.value, org_id)


def _handle_subscription_updated(subscription: dict):
    """Handle customer.subscription.updated — plan changes, renewals."""
    subscription_id = subscription.get("id")
    if not subscription_id:
        return

    tier = _tier_from_subscription(subscription)
    status = subscription.get("status", "active")
    cancel_at_period_end = subscription.get("cancel_at_period_end", False)

    period_start = None
    period_end = None
    if subscription.get("current_period_start"):
        period_start = datetime.fromtimestamp(subscription["current_period_start"], tz=timezone.utc)
    if subscription.get("current_period_end"):
        period_end = datetime.fromtimestamp(subscription["current_period_end"], tz=timezone.utc)

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE org_subscriptions SET
                     plan_tier = %s,
                     status = %s,
                     billing_period_start = COALESCE(%s, billing_period_start),
                     billing_period_end = COALESCE(%s, billing_period_end),
                     cancel_at_period_end = %s,
                     updated_at = CURRENT_TIMESTAMP
                   WHERE stripe_subscription_id = %s""",
                (tier.value, status, period_start, period_end,
                 cancel_at_period_end, subscription_id),
            )
            conn.commit()

    logger.info("[STRIPE] Updated subscription %s to tier=%s status=%s", subscription_id, tier.value, status)


def _handle_subscription_deleted(subscription: dict):
    """Handle customer.subscription.deleted — downgrade to free."""
    subscription_id = subscription.get("id")
    if not subscription_id:
        return

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE org_subscriptions SET
                     plan_tier = 'free',
                     status = 'canceled',
                     stripe_subscription_id = NULL,
                     cancel_at_period_end = FALSE,
                     updated_at = CURRENT_TIMESTAMP
                   WHERE stripe_subscription_id = %s""",
                (subscription_id,),
            )
            conn.commit()

    logger.info("[STRIPE] Subscription %s deleted, downgraded to free", subscription_id)


def _handle_invoice_payment_failed(invoice: dict):
    """Handle invoice.payment_failed — mark subscription as past_due."""
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE org_subscriptions SET status = 'past_due', updated_at = CURRENT_TIMESTAMP "
                "WHERE stripe_subscription_id = %s",
                (subscription_id,),
            )
            conn.commit()

    logger.warning("[STRIPE] Payment failed for subscription %s", subscription_id)


@stripe_webhook_bp.route("/stripe", methods=["POST"])
def handle_stripe_webhook():
    """Process incoming Stripe webhook events."""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    if not sig_header or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Missing signature or webhook secret"}), 400

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        logger.warning("[STRIPE] Invalid webhook signature")
        return jsonify({"error": "Invalid signature"}), 400
    except ValueError:
        logger.warning("[STRIPE] Invalid webhook payload")
        return jsonify({"error": "Invalid payload"}), 400

    event_type = event["type"]
    event_id = event["id"]
    data_object = event["data"]["object"]

    org_id = data_object.get("metadata", {}).get("org_id")

    # Check idempotency without committing — only commit after handler succeeds
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM stripe_events WHERE stripe_event_id = %s",
                (event_id,),
            )
            if cursor.fetchone():
                return jsonify({"status": "already_processed"})

    handlers = {
        "checkout.session.completed": _handle_checkout_completed,
        "customer.subscription.updated": _handle_subscription_updated,
        "customer.subscription.deleted": _handle_subscription_deleted,
        "invoice.payment_failed": _handle_invoice_payment_failed,
    }

    handler = handlers.get(event_type)
    if handler:
        try:
            handler(data_object)
        except Exception:
            logger.exception("[STRIPE] Error handling event %s (%s)", event_id, event_type)
            return jsonify({"error": "Processing failed"}), 500
    else:
        logger.debug("[STRIPE] Unhandled event type: %s", event_type)

    # Record event only after successful processing
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _record_event(cursor, conn, event_id, event_type, org_id, event["data"])

    return jsonify({"status": "ok"})

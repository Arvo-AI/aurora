"""Stripe billing API routes for SaaS mode."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from flask import Blueprint, jsonify, request

from routes.billing.plans import PlanTier, PLAN_LIMITS
from utils.auth.rbac_decorators import require_auth_only
from utils.auth.stateless_auth import get_user_id_from_request, get_org_id_from_request
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing", __name__, url_prefix="/api/billing")

_stripe_key = os.getenv("STRIPE_SECRET_KEY")
if not _stripe_key:
    logger.warning("[BILLING] STRIPE_SECRET_KEY not configured — billing API calls will fail")
stripe.api_key = _stripe_key or ""


def _require_org_admin(user_id: str, org_id: str):
    """Check that user is an admin of the org. Returns error response or None."""
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT role FROM users WHERE id = %s AND org_id = %s",
                (user_id, org_id),
            )
            row = cursor.fetchone()
            if not row or row[0] != "admin":
                return jsonify({"error": "Admin role required for billing operations"}), 403
    return None

STRIPE_PRICE_MAP = {
    "pro_monthly": os.getenv("STRIPE_PRICE_PRO_MONTHLY", ""),
    "pro_yearly": os.getenv("STRIPE_PRICE_PRO_YEARLY", ""),
    "enterprise_monthly": os.getenv("STRIPE_PRICE_ENTERPRISE_MONTHLY", ""),
    "enterprise_yearly": os.getenv("STRIPE_PRICE_ENTERPRISE_YEARLY", ""),
}


def _get_org_subscription(cursor, org_id: str) -> dict | None:
    cursor.execute(
        "SELECT plan_tier, status, stripe_customer_id, stripe_subscription_id, "
        "billing_period_start, billing_period_end, cancel_at_period_end "
        "FROM org_subscriptions WHERE org_id = %s",
        (org_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "plan_tier": row[0],
        "status": row[1],
        "stripe_customer_id": row[2],
        "stripe_subscription_id": row[3],
        "billing_period_start": row[4].isoformat() if row[4] else None,
        "billing_period_end": row[5].isoformat() if row[5] else None,
        "cancel_at_period_end": row[6],
    }


@billing_bp.route("/subscription", methods=["GET"])
@require_auth_only
def get_subscription(user_id: str):
    """Get the current org's subscription details."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 400

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            sub = _get_org_subscription(cursor, org_id)

    if not sub:
        return jsonify({
            "plan_tier": PlanTier.FREE.value,
            "status": "active",
            "limits": PLAN_LIMITS[PlanTier.FREE],
        })

    tier = PlanTier(sub["plan_tier"]) if sub["plan_tier"] in PlanTier.__members__.values() else PlanTier.FREE
    return jsonify({
        "plan_tier": sub["plan_tier"],
        "status": sub["status"],
        "billing_period_start": sub["billing_period_start"],
        "billing_period_end": sub["billing_period_end"],
        "cancel_at_period_end": sub["cancel_at_period_end"],
        "limits": PLAN_LIMITS[tier],
    })


@billing_bp.route("/usage", methods=["GET"])
@require_auth_only
def get_usage(user_id: str):
    """Get the current org's usage for the current billing period."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 400

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT metric_name, usage_count, period_start, period_end "
                "FROM org_usage WHERE org_id = %s "
                "AND period_end >= CURRENT_DATE "
                "ORDER BY metric_name",
                (org_id,),
            )
            rows = cursor.fetchall()

    usage = {}
    for row in rows:
        usage[row[0]] = {
            "count": row[1],
            "period_start": row[2].isoformat(),
            "period_end": row[3].isoformat(),
        }

    return jsonify({"usage": usage})


@billing_bp.route("/checkout", methods=["POST"])
@require_auth_only
def create_checkout_session(user_id: str):
    """Create a Stripe Checkout session for plan upgrade."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 400

    admin_err = _require_org_admin(user_id, org_id)
    if admin_err:
        return admin_err

    data = request.get_json(silent=True) or {}
    price_key = data.get("price_key")
    if price_key not in STRIPE_PRICE_MAP or not STRIPE_PRICE_MAP[price_key]:
        return jsonify({"error": "Invalid price selection"}), 400

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            # Lock the row to prevent duplicate Stripe customer creation
            cursor.execute(
                "SELECT stripe_customer_id FROM org_subscriptions WHERE org_id = %s FOR UPDATE",
                (org_id,),
            )
            locked_row = cursor.fetchone()
            customer_id = locked_row[0] if locked_row and locked_row[0] else None

            if not customer_id:
                cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
                email_row = cursor.fetchone()
                cursor_email = email_row[0] if email_row else None

                customer = stripe.Customer.create(
                    email=cursor_email,
                    metadata={"org_id": org_id, "user_id": user_id},
                )
                customer_id = customer.id

                cursor.execute(
                    "INSERT INTO org_subscriptions (org_id, stripe_customer_id, plan_tier, status) "
                    "VALUES (%s, %s, 'free', 'active') "
                    "ON CONFLICT (org_id) DO UPDATE SET stripe_customer_id = EXCLUDED.stripe_customer_id",
                    (org_id, customer_id),
                )
            conn.commit()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": STRIPE_PRICE_MAP[price_key], "quantity": 1}],
        mode="subscription",
        success_url=f"{frontend_url}/billing?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{frontend_url}/billing?canceled=true",
        metadata={"org_id": org_id},
    )

    return jsonify({"checkout_url": session.url})


@billing_bp.route("/portal", methods=["POST"])
@require_auth_only
def create_portal_session(user_id: str):
    """Create a Stripe Customer Portal session for managing subscription."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 400

    admin_err = _require_org_admin(user_id, org_id)
    if admin_err:
        return admin_err

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            sub = _get_org_subscription(cursor, org_id)

    if not sub or not sub.get("stripe_customer_id"):
        return jsonify({"error": "No billing account found"}), 404

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    session = stripe.billing_portal.Session.create(
        customer=sub["stripe_customer_id"],
        return_url=f"{frontend_url}/billing",
    )

    return jsonify({"portal_url": session.url})


@billing_bp.route("/cancel", methods=["POST"])
@require_auth_only
def cancel_subscription(user_id: str):
    """Cancel subscription at end of billing period."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 400

    admin_err = _require_org_admin(user_id, org_id)
    if admin_err:
        return admin_err

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            sub = _get_org_subscription(cursor, org_id)

    if not sub or not sub.get("stripe_subscription_id"):
        return jsonify({"error": "No active subscription"}), 404

    stripe.Subscription.modify(
        sub["stripe_subscription_id"],
        cancel_at_period_end=True,
    )

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE org_subscriptions SET cancel_at_period_end = TRUE, updated_at = CURRENT_TIMESTAMP "
                "WHERE org_id = %s",
                (org_id,),
            )
            conn.commit()

    return jsonify({"success": True, "message": "Subscription will cancel at end of billing period"})

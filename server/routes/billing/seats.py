"""Per-seat billing — sync org member count to Stripe subscription quantity."""
from __future__ import annotations

import logging

import stripe

from utils.db.connection_pool import db_pool
from utils.flags.feature_flags import is_saas_mode

logger = logging.getLogger(__name__)


def get_org_member_count(org_id: str) -> int:
    """Count active members in an org."""
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM users WHERE org_id = %s AND role != 'disabled'",
                (org_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0


def sync_seat_count(org_id: str) -> bool:
    """Update Stripe subscription quantity to match current org member count.

    Called after member additions/removals. Returns True if sync succeeded.
    Fails silently on free tier (no subscription to update).
    """
    if not is_saas_mode():
        return True

    member_count = get_org_member_count(org_id)
    if member_count < 1:
        member_count = 1

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT stripe_subscription_id, seat_count, plan_tier "
                "FROM org_subscriptions WHERE org_id = %s",
                (org_id,),
            )
            row = cursor.fetchone()

    if not row:
        return True

    subscription_id, current_seats, plan_tier = row

    if plan_tier == "free":
        return True

    if not subscription_id:
        return True

    if current_seats == member_count:
        return True

    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        if not sub.get("items") or not sub["items"].get("data"):
            logger.error("[SEATS] Subscription %s has no items", subscription_id)
            return False

        item_id = sub["items"]["data"][0]["id"]
        stripe.SubscriptionItem.modify(item_id, quantity=member_count)
    except stripe.StripeError as e:
        logger.error("[SEATS] Failed to update seat count for org %s: %s", org_id, e)
        return False

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE org_subscriptions SET seat_count = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE org_id = %s",
                (member_count, org_id),
            )
            conn.commit()

    logger.info("[SEATS] Updated org %s seat count: %d -> %d", org_id, current_seats, member_count)
    return True


def check_seat_limit(org_id: str) -> tuple[bool, int, int]:
    """Check if org can add another member. Returns (allowed, current, limit).

    Free tier: limited by PLAN_LIMITS max_team_members.
    Paid tier: no hard limit — seats auto-scale with billing.
    """
    if not is_saas_mode():
        return True, 0, -1

    from routes.billing.plans import PlanTier, get_plan_limit
    from routes.billing.tier_gate import get_org_tier

    tier = get_org_tier(org_id)
    limit = get_plan_limit(tier, "max_team_members")
    current = get_org_member_count(org_id)

    if limit == -1:
        return True, current, -1

    if tier in (PlanTier.PRO, PlanTier.ENTERPRISE):
        return True, current, -1

    return current < limit, current, limit

"""Tier gating decorator for SaaS plan enforcement."""
from __future__ import annotations

import functools
import logging
from datetime import date

from flask import jsonify

from routes.billing.plans import PlanTier, get_plan_limit, has_feature, PLAN_LIMITS
from utils.auth.stateless_auth import get_org_id_from_request as get_current_org_id
from utils.db.connection_pool import db_pool
from utils.flags.feature_flags import is_saas_mode

logger = logging.getLogger(__name__)


def get_org_tier(org_id: str) -> PlanTier:
    """Get the current plan tier for an org. Returns FREE if not found or not in SaaS mode."""
    if not is_saas_mode():
        return PlanTier.ENTERPRISE  # OSS mode = unlimited

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT plan_tier, status FROM org_subscriptions WHERE org_id = %s",
                    (org_id,),
                )
                row = cursor.fetchone()
                if row and row[1] in ("active", "trialing"):
                    tier_val = row[0]
                    if tier_val in (t.value for t in PlanTier):
                        return PlanTier(tier_val)
        return PlanTier.FREE
    except Exception:
        logger.warning("[TIER] Failed to fetch tier for org %s, defaulting to free", org_id)
        return PlanTier.FREE


def increment_usage(org_id: str, metric_name: str, amount: int = 1) -> int:
    """Increment a usage metric for the current billing period. Returns new count."""
    today = date.today()
    period_start = today.replace(day=1)
    if today.month == 12:
        period_end = today.replace(year=today.year + 1, month=1, day=1)
    else:
        period_end = today.replace(month=today.month + 1, day=1)

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO org_usage (org_id, metric_name, usage_count, period_start, period_end)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (org_id, metric_name, period_start)
                   DO UPDATE SET usage_count = org_usage.usage_count + %s,
                                 updated_at = CURRENT_TIMESTAMP
                   RETURNING usage_count""",
                (org_id, metric_name, amount, period_start, period_end, amount),
            )
            row = cursor.fetchone()
            conn.commit()
            return row[0] if row else amount


def get_usage_count(org_id: str, metric_name: str) -> int:
    """Get current usage count for a metric in the current billing period."""
    today = date.today()
    period_start = today.replace(day=1)

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT usage_count FROM org_usage "
                "WHERE org_id = %s AND metric_name = %s AND period_start = %s",
                (org_id, metric_name, period_start),
            )
            row = cursor.fetchone()
            return row[0] if row else 0


def require_feature(feature_name: str):
    """Decorator that gates a route behind a plan feature."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not is_saas_mode():
                return f(*args, **kwargs)

            org_id = get_current_org_id()
            if not org_id:
                return jsonify({"error": "No organization context"}), 400

            tier = get_org_tier(org_id)
            if not has_feature(tier, feature_name):
                return jsonify({
                    "error": "Feature not available on your plan",
                    "feature": feature_name,
                    "current_plan": tier.value,
                    "upgrade_required": True,
                }), 403

            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_within_limit(metric_name: str, limit_key: str):
    """Decorator that gates a route behind a usage limit."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if not is_saas_mode():
                return f(*args, **kwargs)

            org_id = get_current_org_id()
            if not org_id:
                return jsonify({"error": "No organization context"}), 400

            tier = get_org_tier(org_id)
            limit = get_plan_limit(tier, limit_key)

            if limit == -1:  # unlimited
                return f(*args, **kwargs)

            current = get_usage_count(org_id, metric_name)
            if current >= limit:
                return jsonify({
                    "error": "Usage limit reached",
                    "metric": metric_name,
                    "current": current,
                    "limit": limit,
                    "current_plan": tier.value,
                    "upgrade_required": True,
                }), 429

            return f(*args, **kwargs)
        return wrapper
    return decorator

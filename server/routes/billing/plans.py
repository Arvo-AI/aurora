"""Plan tier definitions and feature gating for SaaS billing."""
from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class PlanTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


PLAN_LIMITS: Dict[PlanTier, Dict[str, int]] = {
    PlanTier.FREE: {
        "max_providers": 2,
        "max_incidents_per_month": 20,
        "max_team_members": 2,
        "rca_depth": 1,
        "max_actions_per_month": 10,
    },
    PlanTier.PRO: {
        "max_providers": 10,
        "max_incidents_per_month": 500,
        "max_team_members": 10,
        "rca_depth": 3,
        "max_actions_per_month": 200,
    },
    PlanTier.ENTERPRISE: {
        "max_providers": -1,  # unlimited
        "max_incidents_per_month": -1,
        "max_team_members": -1,
        "rca_depth": -1,
        "max_actions_per_month": -1,
    },
}

PLAN_FEATURES: Dict[PlanTier, Set[str]] = {
    PlanTier.FREE: {
        "basic_rca",
        "incident_management",
        "provider_connections",
    },
    PlanTier.PRO: {
        "basic_rca",
        "deep_rca",
        "incident_management",
        "provider_connections",
        "email_notifications",
        "sso",
        "custom_actions",
        "correlation",
    },
    PlanTier.ENTERPRISE: {
        "basic_rca",
        "deep_rca",
        "incident_management",
        "provider_connections",
        "email_notifications",
        "sso",
        "custom_actions",
        "correlation",
        "audit_log",
        "dedicated_support",
        "sla",
        "custom_integrations",
    },
}


def get_plan_limit(tier: PlanTier, limit_key: str) -> int:
    limits = PLAN_LIMITS.get(tier, PLAN_LIMITS[PlanTier.FREE])
    return limits.get(limit_key, 0)


def has_feature(tier: PlanTier, feature: str) -> bool:
    features = PLAN_FEATURES.get(tier, PLAN_FEATURES[PlanTier.FREE])
    return feature in features

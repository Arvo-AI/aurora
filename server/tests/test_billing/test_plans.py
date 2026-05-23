"""Tests for routes.billing.plans — tier enum, limits, features."""
from __future__ import annotations

import pytest

from routes.billing.plans import (
    PLAN_FEATURES,
    PLAN_LIMITS,
    PlanTier,
    get_plan_limit,
    has_feature,
)


class TestPlanTierEnum:
    """PlanTier enum has exactly the expected members and string values."""

    def test_enum_values(self):
        assert PlanTier.FREE.value == "free"
        assert PlanTier.PRO.value == "pro"
        assert PlanTier.ENTERPRISE.value == "enterprise"

    def test_enum_is_str(self):
        """PlanTier inherits str so it can be used directly in JSON."""
        assert isinstance(PlanTier.FREE, str)
        assert PlanTier.PRO.value == "pro"
        assert str(PlanTier.PRO.value) == "pro"

    def test_enum_members_count(self):
        """Only three tiers exist."""
        assert len(PlanTier) == 3

    def test_construction_from_value(self):
        assert PlanTier("free") is PlanTier.FREE
        assert PlanTier("pro") is PlanTier.PRO
        assert PlanTier("enterprise") is PlanTier.ENTERPRISE

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            PlanTier("nonexistent")


class TestPlanLimits:
    """PLAN_LIMITS maps every tier to the expected structure."""

    def test_all_tiers_present(self):
        for tier in PlanTier:
            assert tier in PLAN_LIMITS

    def test_free_limits_are_bounded(self):
        free = PLAN_LIMITS[PlanTier.FREE]
        assert free["max_providers"] == 2
        assert free["max_incidents_per_month"] == 20
        assert free["max_team_members"] == 2
        assert free["rca_depth"] == 1
        assert free["max_actions_per_month"] == 10

    def test_pro_limits_are_higher_than_free(self):
        free = PLAN_LIMITS[PlanTier.FREE]
        pro = PLAN_LIMITS[PlanTier.PRO]
        for key in free:
            assert pro[key] > free[key]

    def test_enterprise_limits_are_unlimited(self):
        enterprise = PLAN_LIMITS[PlanTier.ENTERPRISE]
        for key, value in enterprise.items():
            assert value == -1, f"{key} should be -1 (unlimited)"

    def test_all_tiers_have_same_keys(self):
        keys = set(PLAN_LIMITS[PlanTier.FREE].keys())
        for tier in PlanTier:
            assert set(PLAN_LIMITS[tier].keys()) == keys


class TestPlanFeatures:
    """PLAN_FEATURES maps every tier to the correct feature sets."""

    def test_all_tiers_present(self):
        for tier in PlanTier:
            assert tier in PLAN_FEATURES

    def test_free_has_basic_features(self):
        free = PLAN_FEATURES[PlanTier.FREE]
        assert "basic_rca" in free
        assert "incident_management" in free
        assert "provider_connections" in free

    def test_free_lacks_pro_features(self):
        free = PLAN_FEATURES[PlanTier.FREE]
        assert "deep_rca" not in free
        assert "sso" not in free
        assert "custom_actions" not in free

    def test_pro_is_superset_of_free(self):
        assert PLAN_FEATURES[PlanTier.FREE].issubset(PLAN_FEATURES[PlanTier.PRO])

    def test_enterprise_is_superset_of_pro(self):
        assert PLAN_FEATURES[PlanTier.PRO].issubset(PLAN_FEATURES[PlanTier.ENTERPRISE])

    def test_enterprise_exclusive_features(self):
        enterprise = PLAN_FEATURES[PlanTier.ENTERPRISE]
        pro = PLAN_FEATURES[PlanTier.PRO]
        enterprise_only = enterprise - pro
        assert "audit_log" in enterprise_only
        assert "dedicated_support" in enterprise_only
        assert "sla" in enterprise_only
        assert "custom_integrations" in enterprise_only


class TestGetPlanLimit:
    """get_plan_limit returns correct values and sensible defaults."""

    def test_returns_correct_value_for_known_key(self):
        assert get_plan_limit(PlanTier.FREE, "max_providers") == 2
        assert get_plan_limit(PlanTier.PRO, "max_providers") == 10
        assert get_plan_limit(PlanTier.ENTERPRISE, "max_providers") == -1

    def test_returns_zero_for_unknown_key(self):
        assert get_plan_limit(PlanTier.FREE, "nonexistent_key") == 0
        assert get_plan_limit(PlanTier.PRO, "nonexistent_key") == 0

    def test_returns_free_limits_for_unknown_tier(self):
        # If somehow an invalid tier is passed, falls back to FREE
        # This uses the dict .get() fallback
        class FakeTier:
            pass

        result = get_plan_limit(FakeTier, "max_providers")
        assert result == PLAN_LIMITS[PlanTier.FREE]["max_providers"]


class TestHasFeature:
    """has_feature returns correct booleans for plan/feature combos."""

    def test_free_has_basic_rca(self):
        assert has_feature(PlanTier.FREE, "basic_rca") is True

    def test_free_lacks_deep_rca(self):
        assert has_feature(PlanTier.FREE, "deep_rca") is False

    def test_pro_has_deep_rca(self):
        assert has_feature(PlanTier.PRO, "deep_rca") is True

    def test_pro_lacks_audit_log(self):
        assert has_feature(PlanTier.PRO, "audit_log") is False

    def test_enterprise_has_all(self):
        for feature in PLAN_FEATURES[PlanTier.ENTERPRISE]:
            assert has_feature(PlanTier.ENTERPRISE, feature) is True

    def test_nonexistent_feature_returns_false(self):
        assert has_feature(PlanTier.ENTERPRISE, "warp_drive") is False

    def test_unknown_tier_falls_back_to_free(self):
        class FakeTier:
            pass

        assert has_feature(FakeTier, "basic_rca") is True
        assert has_feature(FakeTier, "deep_rca") is False

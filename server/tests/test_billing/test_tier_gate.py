"""Tests for routes.billing.tier_gate — org tier lookup, usage tracking, decorators."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from flask import Flask, jsonify

from routes.billing.plans import PlanTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_db_pool(fetchone_result=None, fetchall_result=None):
    """Build a mock db_pool with configurable results."""

    class MockCursor:
        def __init__(self):
            self.fetchone_result = fetchone_result
            self.fetchall_result = fetchall_result or []
            self.last_query = None
            self.last_params = None

        def execute(self, query, params=None):
            self.last_query = query
            self.last_params = params

        def fetchone(self):
            return self.fetchone_result

        def fetchall(self):
            return self.fetchall_result

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class MockConn:
        def __init__(self):
            self._cursor = MockCursor()

        def cursor(self):
            return self._cursor

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Pool:
        def __init__(self):
            self._conn = MockConn()

        @property
        def cursor_instance(self):
            return self._conn._cursor

        def get_admin_connection(self):
            return self._conn

    return Pool()


def _fresh_tier_gate(db_pool_mock, saas_mode=True):
    """Import tier_gate with mocked dependencies, returning the module."""
    mods_to_evict = [m for m in sys.modules if m.startswith("routes.billing")]
    for mod in mods_to_evict:
        del sys.modules[mod]

    with patch("utils.db.connection_pool.db_pool", db_pool_mock), \
         patch("utils.flags.feature_flags.is_saas_mode", return_value=saas_mode):
        import routes.billing.tier_gate as tg
        return tg


# ---------------------------------------------------------------------------
# Tests: get_org_tier
# ---------------------------------------------------------------------------

class TestGetOrgTier:
    """get_org_tier looks up the org subscription and returns the correct tier."""

    def test_returns_enterprise_when_not_saas_mode(self):
        pool = _make_mock_db_pool()
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=False):
            tg = _fresh_tier_gate(pool, saas_mode=False)
            # Override is_saas_mode for this call
            with patch.object(tg, "is_saas_mode", return_value=False):
                result = tg.get_org_tier("org_123")
        assert result == PlanTier.ENTERPRISE

    def test_returns_free_when_no_subscription_found(self):
        pool = _make_mock_db_pool(fetchone_result=None)
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool, saas_mode=True)
            with patch.object(tg, "is_saas_mode", return_value=True):
                result = tg.get_org_tier("org_123")
        assert result == PlanTier.FREE

    def test_returns_pro_for_active_pro_subscription(self):
        pool = _make_mock_db_pool(fetchone_result=("pro", "active"))
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool, saas_mode=True)
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "db_pool", pool):
                result = tg.get_org_tier("org_456")
        assert result == PlanTier.PRO

    def test_returns_tier_for_trialing_status(self):
        pool = _make_mock_db_pool(fetchone_result=("enterprise", "trialing"))
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool, saas_mode=True)
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "db_pool", pool):
                result = tg.get_org_tier("org_789")
        assert result == PlanTier.ENTERPRISE

    def test_returns_free_for_canceled_status(self):
        pool = _make_mock_db_pool(fetchone_result=("pro", "canceled"))
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool, saas_mode=True)
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "db_pool", pool):
                result = tg.get_org_tier("org_canceled")
        assert result == PlanTier.FREE

    def test_returns_free_on_db_exception(self):
        pool = _make_mock_db_pool()
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool, saas_mode=True)
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "db_pool") as broken_pool:
                broken_pool.get_admin_connection.side_effect = RuntimeError("DB down")
                result = tg.get_org_tier("org_fail")
        assert result == PlanTier.FREE


# ---------------------------------------------------------------------------
# Tests: increment_usage
# ---------------------------------------------------------------------------

class TestIncrementUsage:
    """increment_usage performs an upsert and returns the new count."""

    def test_returns_new_count_from_db(self):
        pool = _make_mock_db_pool(fetchone_result=(5,))
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool)
            with patch.object(tg, "db_pool", pool):
                result = tg.increment_usage("org_1", "max_incidents_per_month", 1)
        assert result == 5

    def test_returns_amount_when_no_row_returned(self):
        pool = _make_mock_db_pool(fetchone_result=None)
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool)
            with patch.object(tg, "db_pool", pool):
                result = tg.increment_usage("org_1", "max_incidents_per_month", 3)
        assert result == 3

    def test_custom_amount(self):
        pool = _make_mock_db_pool(fetchone_result=(10,))
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool)
            with patch.object(tg, "db_pool", pool):
                result = tg.increment_usage("org_1", "max_actions_per_month", 5)
        assert result == 10


# ---------------------------------------------------------------------------
# Tests: get_usage_count
# ---------------------------------------------------------------------------

class TestGetUsageCount:
    """get_usage_count returns current period usage or zero."""

    def test_returns_count_from_db(self):
        pool = _make_mock_db_pool(fetchone_result=(42,))
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool)
            with patch.object(tg, "db_pool", pool):
                result = tg.get_usage_count("org_1", "max_incidents_per_month")
        assert result == 42

    def test_returns_zero_when_no_row(self):
        pool = _make_mock_db_pool(fetchone_result=None)
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=True):
            tg = _fresh_tier_gate(pool)
            with patch.object(tg, "db_pool", pool):
                result = tg.get_usage_count("org_1", "max_incidents_per_month")
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: require_feature decorator
# ---------------------------------------------------------------------------

class TestRequireFeatureDecorator:
    """require_feature gates routes based on plan features."""

    def _make_app_with_decorated_route(self, feature_name, saas_mode=True):
        """Create a Flask app with a route gated by require_feature."""
        pool = _make_mock_db_pool()
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=saas_mode):
            tg = _fresh_tier_gate(pool, saas_mode=saas_mode)

        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/test")
        @tg.require_feature(feature_name)
        def test_route():
            return jsonify({"success": True})

        return app, tg, pool

    def test_passes_through_when_not_saas_mode(self):
        app, tg, pool = self._make_app_with_decorated_route("deep_rca", saas_mode=False)
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=False):
                resp = client.get("/test")
        assert resp.status_code == 200

    def test_returns_400_when_no_org_id(self):
        app, tg, pool = self._make_app_with_decorated_route("deep_rca", saas_mode=True)
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value=None):
                resp = client.get("/test")
        assert resp.status_code == 400

    def test_returns_403_when_feature_not_in_plan(self):
        app, tg, pool = self._make_app_with_decorated_route("deep_rca", saas_mode=True)
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value="org_123"), \
                 patch.object(tg, "get_org_tier", return_value=PlanTier.FREE):
                resp = client.get("/test")
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["feature"] == "deep_rca"
        assert data["upgrade_required"] is True

    def test_allows_when_feature_in_plan(self):
        app, tg, pool = self._make_app_with_decorated_route("basic_rca", saas_mode=True)
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value="org_123"), \
                 patch.object(tg, "get_org_tier", return_value=PlanTier.FREE):
                resp = client.get("/test")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: require_within_limit decorator
# ---------------------------------------------------------------------------

class TestRequireWithinLimitDecorator:
    """require_within_limit gates routes based on usage limits."""

    def _make_app_with_limit_route(self, metric, limit_key, saas_mode=True):
        pool = _make_mock_db_pool()
        with patch("utils.db.connection_pool.db_pool", pool), \
             patch("utils.flags.feature_flags.is_saas_mode", return_value=saas_mode):
            tg = _fresh_tier_gate(pool, saas_mode=saas_mode)

        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.route("/test", methods=["POST"])
        @tg.require_within_limit(metric, limit_key)
        def test_route():
            return jsonify({"success": True})

        return app, tg, pool

    def test_passes_through_when_not_saas_mode(self):
        app, tg, pool = self._make_app_with_limit_route(
            "incidents", "max_incidents_per_month", saas_mode=False
        )
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=False):
                resp = client.post("/test")
        assert resp.status_code == 200

    def test_returns_400_when_no_org_id(self):
        app, tg, pool = self._make_app_with_limit_route(
            "incidents", "max_incidents_per_month"
        )
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value=None):
                resp = client.post("/test")
        assert resp.status_code == 400

    def test_allows_unlimited_tier(self):
        """Enterprise tier (limit=-1) should pass through without checking usage."""
        app, tg, pool = self._make_app_with_limit_route(
            "incidents", "max_incidents_per_month"
        )
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value="org_ent"), \
                 patch.object(tg, "get_org_tier", return_value=PlanTier.ENTERPRISE):
                resp = client.post("/test")
        assert resp.status_code == 200

    def test_allows_under_limit(self):
        """Usage below limit should succeed (atomic increment returns a row)."""
        app, tg, pool = self._make_app_with_limit_route(
            "incidents", "max_incidents_per_month"
        )
        # cursor.fetchone returns (5,) meaning increment succeeded
        pool.cursor_instance.fetchone_result = (5,)
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value="org_free"), \
                 patch.object(tg, "get_org_tier", return_value=PlanTier.FREE), \
                 patch.object(tg, "db_pool", pool):
                resp = client.post("/test")
        assert resp.status_code == 200

    def test_rejects_at_limit(self):
        """When atomic increment returns None (limit reached), return 429."""
        app, tg, pool = self._make_app_with_limit_route(
            "incidents", "max_incidents_per_month"
        )
        # cursor.fetchone returns None meaning limit was hit
        pool.cursor_instance.fetchone_result = None
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value="org_free"), \
                 patch.object(tg, "get_org_tier", return_value=PlanTier.FREE), \
                 patch.object(tg, "db_pool", pool), \
                 patch.object(tg, "get_usage_count", return_value=20):
                resp = client.post("/test")
        assert resp.status_code == 429
        data = resp.get_json()
        assert data["metric"] == "incidents"
        assert data["limit"] == 20
        assert data["upgrade_required"] is True

    def test_rejects_zero_limit(self):
        """If get_plan_limit returns 0, the route is blocked immediately (no DB call)."""
        app, tg, pool = self._make_app_with_limit_route(
            "incidents", "nonexistent_limit_key"
        )
        with app.test_client() as client:
            with patch.object(tg, "is_saas_mode", return_value=True), \
                 patch.object(tg, "get_current_org_id", return_value="org_free"), \
                 patch.object(tg, "get_org_tier", return_value=PlanTier.FREE):
                resp = client.post("/test")
        assert resp.status_code == 429
        data = resp.get_json()
        assert data["limit"] == 0

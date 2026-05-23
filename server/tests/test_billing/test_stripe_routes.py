"""Tests for routes.billing.stripe_routes — subscription, checkout, cancel endpoints."""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from flask import Flask

from routes.billing.plans import PlanTier, PLAN_LIMITS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_db_pool():
    """Build a mock db_pool with multi-call support."""
    call_index = {"n": 0}
    results_queue = []

    class MockCursor:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=None):
            self.queries.append((query, params))

        def fetchone(self):
            idx = call_index["n"]
            call_index["n"] += 1
            if idx < len(results_queue):
                return results_queue[idx]
            return None

        def fetchall(self):
            return []

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
            self._results_queue = results_queue
            self._call_index = call_index

        @property
        def cursor_instance(self):
            return self._conn._cursor

        def set_results(self, *results):
            self._results_queue.clear()
            self._results_queue.extend(results)
            self._call_index["n"] = 0

        def get_admin_connection(self):
            return self._conn

    return Pool()


def _fresh_routes_module(db_pool_mock, stripe_key="sk_test_123"):
    """Import stripe_routes with mocked dependencies."""
    mods_to_evict = [m for m in sys.modules if m.startswith("routes.billing")]
    for mod in mods_to_evict:
        del sys.modules[mod]

    # Ensure stripe module is a mock
    stripe_mock = MagicMock()
    sys.modules["stripe"] = stripe_mock

    # Stub auth decorators
    auth_mock = MagicMock()

    def passthrough_decorator(f):
        """A no-op decorator that passes user_id='test_user'."""
        from functools import wraps

        @wraps(f)
        def wrapper(*args, **kwargs):
            return f("test_user", *args, **kwargs)
        return wrapper

    auth_mock.require_auth_only = passthrough_decorator

    with patch("utils.db.connection_pool.db_pool", db_pool_mock), \
         patch.dict("os.environ", {
             "STRIPE_SECRET_KEY": stripe_key,
             "STRIPE_PRICE_PRO_MONTHLY": "price_pro_m",
             "STRIPE_PRICE_PRO_YEARLY": "price_pro_y",
             "STRIPE_PRICE_ENTERPRISE_MONTHLY": "price_ent_m",
             "STRIPE_PRICE_ENTERPRISE_YEARLY": "price_ent_y",
             "FRONTEND_URL": "http://localhost:3000",
         }), \
         patch.dict(sys.modules, {"utils.auth.rbac_decorators": auth_mock}):
        import routes.billing.stripe_routes as sr
        return sr, stripe_mock


# ---------------------------------------------------------------------------
# Tests: GET /subscription
# ---------------------------------------------------------------------------

class TestGetSubscription:
    """GET /api/billing/subscription returns plan info."""

    def test_returns_free_tier_when_no_subscription(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # No subscription row found
        pool.set_results(None)

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_free"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.get("/api/billing/subscription")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["plan_tier"] == "free"
        assert data["status"] == "active"
        assert data["limits"] == PLAN_LIMITS[PlanTier.FREE]

    def test_returns_existing_subscription_details(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # Simulate a pro subscription row
        from datetime import date
        pool.set_results(("pro", "active", "cus_123", "sub_456", date(2024, 1, 1), date(2024, 2, 1), False))

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_pro"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.get("/api/billing/subscription")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["plan_tier"] == "pro"
        assert data["status"] == "active"
        assert data["limits"] == PLAN_LIMITS[PlanTier.PRO]

    def test_returns_400_when_no_org_context(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value=None):
                resp = client.get("/api/billing/subscription")

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests: POST /checkout
# ---------------------------------------------------------------------------

class TestCreateCheckout:
    """POST /api/billing/checkout creates a Stripe checkout session."""

    def test_requires_admin_role(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # User is not admin (role check returns 'viewer')
        pool.set_results(("viewer",))

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post(
                    "/api/billing/checkout",
                    data=json.dumps({"price_key": "pro_monthly"}),
                    content_type="application/json",
                )

        assert resp.status_code == 403
        assert "Admin role required" in resp.get_json()["error"]

    def test_rejects_invalid_price_key(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # User is admin
        pool.set_results(("admin",))

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post(
                    "/api/billing/checkout",
                    data=json.dumps({"price_key": "invalid_key"}),
                    content_type="application/json",
                )

        assert resp.status_code == 400
        assert "Invalid price selection" in resp.get_json()["error"]

    def test_creates_session_for_admin_with_valid_price(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # Admin check passes, existing customer found
        pool.set_results(("admin",), ("cus_existing",))

        # Mock Stripe checkout session
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/session_abc"
        stripe_mock.checkout.Session.create.return_value = mock_session
        stripe_mock.StripeError = type("StripeError", (Exception,), {})

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post(
                    "/api/billing/checkout",
                    data=json.dumps({"price_key": "pro_monthly"}),
                    content_type="application/json",
                )

        assert resp.status_code == 200
        assert resp.get_json()["checkout_url"] == "https://checkout.stripe.com/session_abc"


# ---------------------------------------------------------------------------
# Tests: POST /cancel
# ---------------------------------------------------------------------------

class TestCancelSubscription:
    """POST /api/billing/cancel cancels at period end."""

    def test_requires_active_subscription(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # Admin check passes, but no subscription found
        pool.set_results(("admin",), None)

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post("/api/billing/cancel")

        assert resp.status_code == 404
        assert "No active subscription" in resp.get_json()["error"]

    def test_requires_admin(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # Non-admin
        pool.set_results(("member",))

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post("/api/billing/cancel")

        assert resp.status_code == 403

    def test_cancels_subscription_at_period_end(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # Admin check passes, then FOR UPDATE returns stripe_subscription_id
        pool.set_results(
            ("admin",),
            ("sub_cancel_me",),
        )

        stripe_mock.StripeError = type("StripeError", (Exception,), {})
        stripe_mock.Subscription.modify.return_value = MagicMock()

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post("/api/billing/cancel")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        stripe_mock.Subscription.modify.assert_called_once_with(
            "sub_cancel_me", cancel_at_period_end=True
        )

    def test_returns_502_on_stripe_error(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        pool.set_results(
            ("admin",),
            ("sub_err",),
        )

        stripe_error_class = type("StripeError", (Exception,), {})
        stripe_mock.StripeError = stripe_error_class
        stripe_mock.Subscription.modify.side_effect = stripe_error_class("network error")

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post("/api/billing/cancel")

        assert resp.status_code == 502
        assert "Payment provider error" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Tests: GET /usage
# ---------------------------------------------------------------------------

class TestGetUsage:
    """GET /api/billing/usage returns current period metrics."""

    def test_returns_usage_data(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        from datetime import date
        # Mock fetchall to return usage rows
        pool._conn._cursor.fetchall = lambda: [
            ("max_incidents_per_month", 15, date(2024, 1, 1), date(2024, 2, 1)),
            ("max_actions_per_month", 3, date(2024, 1, 1), date(2024, 2, 1)),
        ]

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.get("/api/billing/usage")

        assert resp.status_code == 200
        data = resp.get_json()
        assert "max_incidents_per_month" in data["usage"]
        assert data["usage"]["max_incidents_per_month"]["count"] == 15

    def test_returns_400_when_no_org_context(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value=None):
                resp = client.get("/api/billing/usage")

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests: POST /portal
# ---------------------------------------------------------------------------

class TestCreatePortalSession:
    """POST /api/billing/portal creates a Stripe Customer Portal session."""

    def test_requires_billing_account(self):
        pool = _make_mock_db_pool()
        sr, stripe_mock = _fresh_routes_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sr.billing_bp)

        # Admin check passes, but no subscription with customer_id
        pool.set_results(("admin",), None)

        with app.test_client() as client:
            with patch.object(sr, "get_org_id_from_request", return_value="org_1"), \
                 patch.object(sr, "db_pool", pool):
                resp = client.post("/api/billing/portal")

        assert resp.status_code == 404
        assert "No billing account" in resp.get_json()["error"]

"""Tests for routes.billing.stripe_webhook — signature, idempotency, handlers."""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch, call

import pytest

from flask import Flask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_db_pool():
    """Build a mock db_pool with multi-call support."""
    call_index = {"n": 0}
    results_queue = []

    class MockCursor:
        def __init__(self):
            self.last_query = None
            self.last_params = None
            self.queries = []
            self.rowcount = 1  # Default: pretend 1 row was affected

        def execute(self, query, params=None):
            self.last_query = query
            self.last_params = params
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
            """Set sequential fetchone results."""
            self._results_queue.clear()
            self._results_queue.extend(results)
            self._call_index["n"] = 0

        def get_admin_connection(self):
            return self._conn

    return Pool()


def _build_stripe_event(event_type, event_id="evt_test_123", data_object=None):
    """Construct a Stripe event dict."""
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": data_object or {},
        },
    }


def _fresh_webhook_module(db_pool_mock, webhook_secret="whsec_test"):
    """Import stripe_webhook with mocked dependencies."""
    mods_to_evict = [m for m in sys.modules if m.startswith("routes.billing")]
    for mod in mods_to_evict:
        del sys.modules[mod]

    # Ensure stripe module is a mock
    stripe_mock = MagicMock()
    sys.modules["stripe"] = stripe_mock

    with patch("utils.db.connection_pool.db_pool", db_pool_mock), \
         patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": webhook_secret}):
        import routes.billing.stripe_webhook as sw
        return sw, stripe_mock


# ---------------------------------------------------------------------------
# Tests: Signature verification
# ---------------------------------------------------------------------------

class TestSignatureVerification:
    """Webhook rejects requests with bad/missing signatures."""

    def test_missing_signature_header_returns_400(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data='{"test": true}',
                content_type="application/json",
                # No Stripe-Signature header
            )
        assert resp.status_code == 400
        assert "Missing signature" in resp.get_json()["error"]

    def test_invalid_signature_returns_400(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        # Make construct_event raise SignatureVerificationError
        sig_error = type("SignatureVerificationError", (Exception,), {})
        stripe_mock.SignatureVerificationError = sig_error
        stripe_mock.Webhook.construct_event.side_effect = sig_error("bad sig")

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data='{"test": true}',
                content_type="application/json",
                headers={"Stripe-Signature": "t=1,v1=bad_sig"},
            )
        assert resp.status_code == 400
        assert "Invalid signature" in resp.get_json()["error"]

    def test_invalid_payload_returns_400(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        sig_error = type("SignatureVerificationError", (Exception,), {})
        stripe_mock.SignatureVerificationError = sig_error
        stripe_mock.Webhook.construct_event.side_effect = ValueError("bad json")

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data="not json at all",
                content_type="application/json",
                headers={"Stripe-Signature": "t=1,v1=something"},
            )
        assert resp.status_code == 400
        assert "Invalid payload" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Tests: Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Duplicate events are detected and not reprocessed."""

    def test_duplicate_event_returns_already_processed(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        event = _build_stripe_event("customer.subscription.updated", "evt_dup_1")
        stripe_mock.SignatureVerificationError = type("SVE", (Exception,), {})
        stripe_mock.Webhook.construct_event.return_value = event

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        # First fetchone returns None (no id from INSERT => already exists)
        pool.set_results(None)

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data=json.dumps(event),
                content_type="application/json",
                headers={"Stripe-Signature": "t=1,v1=valid"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "already_processed"

    def test_new_event_is_claimed_and_processed(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        event = _build_stripe_event(
            "customer.subscription.deleted",
            "evt_new_1",
            data_object={"id": "sub_123", "metadata": {}},
        )
        stripe_mock.SignatureVerificationError = type("SVE", (Exception,), {})
        stripe_mock.Webhook.construct_event.return_value = event

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        # First fetchone: claimed (returns id), subsequent: handler queries
        pool.set_results((1,), None)

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data=json.dumps(event),
                content_type="application/json",
                headers={"Stripe-Signature": "t=1,v1=valid"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Tests: Event handlers update DB
# ---------------------------------------------------------------------------

class TestCheckoutCompletedHandler:
    """_handle_checkout_completed activates the subscription."""

    def test_creates_subscription_record(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        # Stripe resources act as both dicts and objects; use a simple class.
        class StripeSubscription(dict):
            def __init__(self, data):
                super().__init__(data)
                for k, v in data.items():
                    setattr(self, k, v)

        mock_sub = StripeSubscription({
            "current_period_start": 1700000000,
            "current_period_end": 1702592000,
            "items": {"data": [{"price": {"id": "price_pro_monthly"}}]},
        })
        stripe_mock.Subscription.retrieve.return_value = mock_sub
        stripe_mock.StripeError = type("StripeError", (Exception,), {})

        # Patch PRICE_TO_TIER
        with patch.object(sw, "PRICE_TO_TIER", {"price_pro_monthly": sw.PlanTier.PRO}):
            sw._handle_checkout_completed({
                "metadata": {"org_id": "org_abc"},
                "subscription": "sub_123",
                "customer": "cus_456",
            })

        # Verify an INSERT was attempted
        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("INSERT INTO org_subscriptions" in q for q in queries)

    def test_skips_when_missing_org_id(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        sw._handle_checkout_completed({
            "metadata": {},
            "subscription": "sub_123",
            "customer": "cus_456",
        })

        # No DB queries should have been made
        assert len(pool.cursor_instance.queries) == 0


class TestSubscriptionUpdatedHandler:
    """_handle_subscription_updated updates plan_tier and status."""

    def test_updates_subscription_fields(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        with patch.object(sw, "PRICE_TO_TIER", {"price_ent": sw.PlanTier.ENTERPRISE}):
            sw._handle_subscription_updated({
                "id": "sub_789",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": 1700000000,
                "current_period_end": 1702592000,
                "items": {"data": [{"price": {"id": "price_ent"}}]},
            })

        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("UPDATE org_subscriptions" in q for q in queries)

    def test_skips_when_no_subscription_id(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        sw._handle_subscription_updated({"status": "active"})
        assert len(pool.cursor_instance.queries) == 0


class TestSubscriptionDeletedHandler:
    """_handle_subscription_deleted downgrades to free."""

    def test_sets_plan_to_free_and_canceled(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        sw._handle_subscription_deleted({"id": "sub_to_delete"})

        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("plan_tier = 'free'" in q and "status = 'canceled'" in q for q in queries)


class TestInvoicePaymentFailedHandler:
    """_handle_invoice_payment_failed marks subscription as past_due."""

    def test_marks_past_due(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        sw._handle_invoice_payment_failed({"subscription": "sub_pastdue"})

        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("status = 'past_due'" in q for q in queries)


# ---------------------------------------------------------------------------
# Tests: Failed events
# ---------------------------------------------------------------------------

class TestFailedEvents:
    """Handler exceptions cause the event to be marked as 'failed'."""

    def test_handler_exception_marks_event_failed(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        event = _build_stripe_event(
            "checkout.session.completed",
            "evt_fail_1",
            data_object={"metadata": {"org_id": "org_x"}, "subscription": "sub_x", "customer": "cus_x"},
        )
        stripe_mock.SignatureVerificationError = type("SVE", (Exception,), {})
        stripe_mock.Webhook.construct_event.return_value = event
        stripe_mock.StripeError = type("StripeError", (Exception,), {})
        stripe_mock.Subscription.retrieve.side_effect = RuntimeError("stripe down")

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        # Idempotency claim succeeds
        pool.set_results((1,))

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data=json.dumps(event),
                content_type="application/json",
                headers={"Stripe-Signature": "t=1,v1=valid"},
            )
        assert resp.status_code == 500

        # Verify status='failed' was written
        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("status = 'failed'" in q for q in queries)


# ---------------------------------------------------------------------------
# Tests: Unhandled event types pass through
# ---------------------------------------------------------------------------

class TestUnhandledEvents:
    """Unknown event types are acknowledged without error."""

    def test_unhandled_event_returns_ok(self):
        pool = _make_mock_db_pool()
        sw, stripe_mock = _fresh_webhook_module(pool)

        event = _build_stripe_event("some.unknown.event", "evt_unknown_1")
        stripe_mock.SignatureVerificationError = type("SVE", (Exception,), {})
        stripe_mock.Webhook.construct_event.return_value = event

        app = Flask(__name__)
        app.register_blueprint(sw.stripe_webhook_bp)

        # Idempotency claim succeeds
        pool.set_results((1,))

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/stripe",
                data=json.dumps(event),
                content_type="application/json",
                headers={"Stripe-Signature": "t=1,v1=valid"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

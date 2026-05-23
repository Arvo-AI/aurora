"""Tests for routes.billing.clerk_webhook — Svix signature, user lifecycle handlers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from flask import Flask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_WEBHOOK_SECRET = base64.b64encode(b"test_secret_key_32bytes_long!!!!").decode()
TEST_WEBHOOK_SECRET_PREFIXED = f"whsec_{TEST_WEBHOOK_SECRET}"


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


def _fresh_clerk_module(db_pool_mock, webhook_secret=TEST_WEBHOOK_SECRET_PREFIXED):
    """Import clerk_webhook with mocked dependencies."""
    mods_to_evict = [m for m in sys.modules if m.startswith("routes.billing")]
    for mod in mods_to_evict:
        del sys.modules[mod]

    with patch("utils.db.connection_pool.db_pool", db_pool_mock), \
         patch.dict("os.environ", {"CLERK_WEBHOOK_SECRET": webhook_secret}):
        import routes.billing.clerk_webhook as cw
        return cw


def _sign_payload(payload: bytes, svix_id: str, timestamp: int, secret: str) -> str:
    """Compute a valid Svix signature for test payloads."""
    if secret.startswith("whsec_"):
        secret = secret[6:]
    secret_bytes = base64.b64decode(secret)
    to_sign = f"{svix_id}.{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    signature = base64.b64encode(
        hmac.HMAC(secret_bytes, to_sign, hashlib.sha256).digest()
    ).decode("utf-8")
    return f"v1,{signature}"


def _make_clerk_event(event_type: str, data: dict) -> dict:
    return {"type": event_type, "data": data}


# ---------------------------------------------------------------------------
# Tests: Svix signature verification
# ---------------------------------------------------------------------------

class TestSvixSignatureVerification:
    """_verify_clerk_signature validates Svix headers correctly."""

    def test_valid_signature_accepted(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        payload = json.dumps({"type": "user.created", "data": {}}).encode()
        svix_id = "msg_test_123"
        timestamp = int(time.time())
        signature = _sign_payload(payload, svix_id, timestamp, TEST_WEBHOOK_SECRET_PREFIXED)

        headers = {
            "svix-id": svix_id,
            "svix-timestamp": str(timestamp),
            "svix-signature": signature,
        }

        result = cw._verify_clerk_signature(payload, headers)
        assert result is True

    def test_invalid_signature_rejected(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        payload = json.dumps({"type": "user.created", "data": {}}).encode()
        svix_id = "msg_test_456"
        timestamp = int(time.time())

        headers = {
            "svix-id": svix_id,
            "svix-timestamp": str(timestamp),
            "svix-signature": "v1,invalid_signature_here",
        }

        result = cw._verify_clerk_signature(payload, headers)
        assert result is False

    def test_expired_timestamp_rejected(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        payload = json.dumps({"type": "user.created", "data": {}}).encode()
        svix_id = "msg_test_789"
        # 10 minutes old (> 5 minute tolerance)
        timestamp = int(time.time()) - 600
        signature = _sign_payload(payload, svix_id, timestamp, TEST_WEBHOOK_SECRET_PREFIXED)

        headers = {
            "svix-id": svix_id,
            "svix-timestamp": str(timestamp),
            "svix-signature": signature,
        }

        result = cw._verify_clerk_signature(payload, headers)
        assert result is False

    def test_missing_headers_rejected(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        payload = b'{"type": "user.created"}'
        # Missing svix-id
        headers = {
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,something",
        }
        assert cw._verify_clerk_signature(payload, headers) is False

        # Missing svix-timestamp
        headers = {
            "svix-id": "msg_1",
            "svix-signature": "v1,something",
        }
        assert cw._verify_clerk_signature(payload, headers) is False

        # Missing svix-signature
        headers = {
            "svix-id": "msg_1",
            "svix-timestamp": str(int(time.time())),
        }
        assert cw._verify_clerk_signature(payload, headers) is False

    def test_no_webhook_secret_configured_rejected(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool, webhook_secret="")

        payload = b'{"type": "user.created"}'
        headers = {
            "svix-id": "msg_1",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,something",
        }
        assert cw._verify_clerk_signature(payload, headers) is False

    def test_multiple_signatures_one_valid(self):
        """Svix can include multiple signatures; at least one must match."""
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        payload = json.dumps({"type": "user.updated", "data": {}}).encode()
        svix_id = "msg_multi"
        timestamp = int(time.time())
        valid_sig = _sign_payload(payload, svix_id, timestamp, TEST_WEBHOOK_SECRET_PREFIXED)

        headers = {
            "svix-id": svix_id,
            "svix-timestamp": str(timestamp),
            "svix-signature": f"v1,invalid_one {valid_sig}",
        }

        result = cw._verify_clerk_signature(payload, headers)
        assert result is True


# ---------------------------------------------------------------------------
# Tests: Webhook route integration
# ---------------------------------------------------------------------------

class TestClerkWebhookRoute:
    """The /api/webhooks/clerk endpoint validates signature and dispatches."""

    def _post_webhook(self, cw, app, event, valid_sig=True):
        """POST a signed Clerk webhook event."""
        payload = json.dumps(event).encode()
        svix_id = "msg_route_test"
        timestamp = int(time.time())

        if valid_sig:
            signature = _sign_payload(payload, svix_id, timestamp, TEST_WEBHOOK_SECRET_PREFIXED)
        else:
            signature = "v1,bogus"

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/clerk",
                data=payload,
                content_type="application/json",
                headers={
                    "svix-id": svix_id,
                    "svix-timestamp": str(timestamp),
                    "svix-signature": signature,
                },
            )
        return resp

    def test_invalid_signature_returns_400(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        app = Flask(__name__)
        app.register_blueprint(cw.clerk_webhook_bp)

        event = _make_clerk_event("user.created", {"id": "user_123"})
        resp = self._post_webhook(cw, app, event, valid_sig=False)
        assert resp.status_code == 400

    def test_invalid_json_returns_400(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        app = Flask(__name__)
        app.register_blueprint(cw.clerk_webhook_bp)

        # Send invalid JSON with a valid signature for the raw bytes
        raw = b"not valid json {"
        svix_id = "msg_bad_json"
        timestamp = int(time.time())
        signature = _sign_payload(raw, svix_id, timestamp, TEST_WEBHOOK_SECRET_PREFIXED)

        with app.test_client() as client:
            resp = client.post(
                "/api/webhooks/clerk",
                data=raw,
                content_type="application/json",
                headers={
                    "svix-id": svix_id,
                    "svix-timestamp": str(timestamp),
                    "svix-signature": signature,
                },
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests: user.created handler
# ---------------------------------------------------------------------------

class TestUserCreatedHandler:
    """_handle_user_created creates user, org, and subscription."""

    def test_creates_user_org_and_subscription(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        # First fetchone: org insert returns id; second: not needed
        pool.set_results(("org_new_uuid",))

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_created({
                "id": "user_clerk_1",
                "email_addresses": [
                    {"id": "email_1", "email_address": "alice@example.com"},
                ],
                "primary_email_address_id": "email_1",
                "first_name": "Alice",
                "last_name": "Smith",
            })

        queries = [q for q, _ in pool.cursor_instance.queries]
        # Should insert user
        assert any("INSERT INTO users" in q for q in queries)
        # Should insert org
        assert any("INSERT INTO organizations" in q for q in queries)
        # Should insert free subscription
        assert any("INSERT INTO org_subscriptions" in q for q in queries)

    def test_skips_when_missing_id(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_created({
                "email_addresses": [{"id": "e1", "email_address": "x@y.com"}],
                "primary_email_address_id": "e1",
            })

        assert len(pool.cursor_instance.queries) == 0

    def test_falls_back_to_first_email_if_no_primary(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)
        pool.set_results(("org_id",))

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_created({
                "id": "user_no_primary",
                "email_addresses": [
                    {"id": "e_other", "email_address": "fallback@example.com"},
                ],
                "primary_email_address_id": "e_nonexistent",
                "first_name": "",
                "last_name": "",
            })

        # Should still proceed (fallback to first email)
        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("INSERT INTO users" in q for q in queries)


# ---------------------------------------------------------------------------
# Tests: user.updated handler
# ---------------------------------------------------------------------------

class TestUserUpdatedHandler:
    """_handle_user_updated syncs changed data."""

    def test_updates_email_and_name(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_updated({
                "id": "user_update_1",
                "email_addresses": [
                    {"id": "e1", "email_address": "new@example.com"},
                ],
                "primary_email_address_id": "e1",
                "first_name": "Bob",
                "last_name": "Jones",
            })

        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("UPDATE users SET" in q for q in queries)

    def test_skips_when_no_user_id(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_updated({
                "email_addresses": [{"id": "e1", "email_address": "x@y.com"}],
                "primary_email_address_id": "e1",
            })

        assert len(pool.cursor_instance.queries) == 0

    def test_skips_update_when_no_fields_changed(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_updated({
                "id": "user_no_change",
                "email_addresses": [],
                "primary_email_address_id": None,
                "first_name": "",
                "last_name": "",
            })

        # No UPDATE should be issued since both email and name are empty
        queries = [q for q, _ in pool.cursor_instance.queries]
        assert not any("UPDATE users" in q for q in queries)


# ---------------------------------------------------------------------------
# Tests: user.deleted handler
# ---------------------------------------------------------------------------

class TestUserDeletedHandler:
    """_handle_user_deleted disables the user."""

    def test_sets_role_to_disabled(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_deleted({"id": "user_to_delete"})

        queries = [q for q, _ in pool.cursor_instance.queries]
        assert any("role = 'disabled'" in q for q in queries)

    def test_skips_when_no_user_id(self):
        pool = _make_mock_db_pool()
        cw = _fresh_clerk_module(pool)

        with patch.object(cw, "db_pool", pool):
            cw._handle_user_deleted({})

        assert len(pool.cursor_instance.queries) == 0

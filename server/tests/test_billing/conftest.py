"""Shared fixtures for billing tests.

Env vars, sys.path insertion, and heavy-package stubs are handled by the
parent ``server/tests/conftest.py`` which pytest loads automatically.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Flask module eviction -- ensures Werkzeug LocalProxy objects are rebound to
# whatever Flask app the current test creates, not one from a prior test file.
# ---------------------------------------------------------------------------
_flask_mods = [m for m in sys.modules if m == "flask" or m.startswith("flask.")]
for _mod in _flask_mods:
    del sys.modules[_mod]

# Ensure stripe is mocked before billing modules import it
if "stripe" not in sys.modules:
    sys.modules["stripe"] = MagicMock()


@pytest.fixture
def mock_db_pool():
    """Provide a mock db_pool with configurable cursor results.

    Usage:
        mock_db_pool.fetchone_result = (value,)
        mock_db_pool.fetchall_result = [(row1,), (row2,)]
    """

    class MockCursor:
        def __init__(self):
            self.fetchone_result = None
            self.fetchall_result = []
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

    class MockConnection:
        def __init__(self, cursor_instance):
            self._cursor = cursor_instance

        def cursor(self):
            return self._cursor

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class MockDBPool:
        def __init__(self):
            self._cursor = MockCursor()
            self._conn = MockConnection(self._cursor)

        @property
        def fetchone_result(self):
            return self._cursor.fetchone_result

        @fetchone_result.setter
        def fetchone_result(self, value):
            self._cursor.fetchone_result = value

        @property
        def fetchall_result(self):
            return self._cursor.fetchall_result

        @fetchall_result.setter
        def fetchall_result(self, value):
            self._cursor.fetchall_result = value

        @property
        def cursor(self):
            return self._cursor

        def get_admin_connection(self):
            return self._conn

    return MockDBPool()


@pytest.fixture
def billing_app(mock_db_pool):
    """Minimal Flask app with billing blueprints registered."""
    # Evict modules so fresh import picks up patches
    mods_to_evict = [
        m for m in sys.modules
        if m.startswith(("routes.billing",))
    ]
    for mod in mods_to_evict:
        del sys.modules[mod]

    # Stub heavy dependencies
    for heavy in (
        "celery_config", "celery", "weaviate", "openai", "anthropic",
        "chat.background.task", "chat.background.summarization",
        "routes.audit_routes",
    ):
        if heavy not in sys.modules:
            sys.modules[heavy] = MagicMock()

    sys.modules.setdefault("routes.audit_routes", MagicMock()).record_audit_event = MagicMock()

    with patch("utils.db.connection_pool.db_pool", mock_db_pool):
        from flask import Flask

        application = Flask(__name__)
        application.config["TESTING"] = True

        from routes.billing.stripe_routes import billing_bp
        from routes.billing.stripe_webhook import stripe_webhook_bp
        from routes.billing.clerk_webhook import clerk_webhook_bp

        application.register_blueprint(billing_bp)
        application.register_blueprint(stripe_webhook_bp)
        application.register_blueprint(clerk_webhook_bp)

        yield application


@pytest.fixture
def client(billing_app):
    """Flask test client for billing routes."""
    return billing_app.test_client()

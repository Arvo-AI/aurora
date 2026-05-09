"""Shared fixtures for server/tests/auth/.

The ``app`` fixture rebuilds the incidents blueprint from scratch on every test
so Werkzeug LocalProxy objects (``request``, ``jsonify``, …) are always bound
to the Flask instance created in the current test run.
"""

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def app():
    """Minimal Flask app with the incidents blueprint registered.

    Force-evicts and re-imports the route module and its Flask-dependent
    transitive dependencies on every fixture instantiation, so the
    Werkzeug LocalProxy objects inside them are bound to the Flask instance
    created in *this* test run rather than one from a prior test file.
    """
    for _mod in list(sys.modules):
        if _mod.startswith("routes.") or _mod.startswith("utils.auth.rbac"):
            del sys.modules[_mod]

    for heavy in (
        "celery_config", "celery", "weaviate", "openai", "anthropic",
        "chat.background.task", "chat.background.summarization",
        "routes.audit_routes",
    ):
        if heavy not in sys.modules:
            sys.modules[heavy] = MagicMock()

    sys.modules["routes.audit_routes"].record_audit_event = MagicMock()

    from flask import Flask as _Flask  # fresh after eviction
    from routes.incidents_routes import incidents_bp  # fresh after eviction

    application = _Flask(__name__)
    application.register_blueprint(incidents_bp)
    # TESTING=True enables exception propagation for cleaner assertions.
    # Aurora has no Flask-side CSRF middleware, so this does not disable any
    # CSRF protection — the trust boundary is the Next.js proxy layer.
    application.config["TESTING"] = True  # NOSONAR
    return application


@pytest.fixture
def client(app):
    return app.test_client()

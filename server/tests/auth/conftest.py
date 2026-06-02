"""Shared fixtures for server/tests/auth/.

The ``app`` fixture rebuilds the incidents blueprint from scratch on every test
so Werkzeug LocalProxy objects (``request``, ``jsonify``, …) are always bound
to the Flask instance created in the current test run.

Env vars, sys.path insertion, and heavy-package stubs are handled by the
parent ``server/tests/conftest.py`` which pytest loads automatically.
"""

import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Flask module eviction — ensures Werkzeug LocalProxy objects are rebound to
# whatever Flask app the current test creates, not one from a prior test file.
# ---------------------------------------------------------------------------

_flask_mods = [m for m in sys.modules if m == "flask" or m.startswith("flask.")]
for _mod in _flask_mods:
    del sys.modules[_mod]


@pytest.fixture
def app():
    """Minimal Flask app with the incidents blueprint registered.

    Force-evicts and re-imports the route module and its Flask-dependent
    transitive dependencies on every fixture instantiation, so the
    Werkzeug LocalProxy objects inside them are bound to the Flask instance
    created in *this* test run rather than one from a prior test file.
    """
    mods_to_evict = [
        m for m in sys.modules
        if m.startswith(("routes.", "utils.auth.rbac"))
    ]
    for _mod in mods_to_evict:
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

    application = _Flask(__name__)  # NOSONAR
    application.register_blueprint(incidents_bp)
    return application


@pytest.fixture
def client(app):
    return app.test_client()

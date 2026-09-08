"""Fixtures for the Slack thread-consolidation tests (no DB, no network).

Stubs ``routes.slack.slack_events_helpers`` in sys.modules so the service's
function-local imports resolve without pulling the Flask routes package.
sys.path and the inert POSTGRES_* env come from the root tests/conftest.py.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from utils.notifications import dispatcher, slack_notification_service, slack_threading

from .slack_fakes import FakePool, FakeSlackClient, folded_incident, standalone_incident


@pytest.fixture
def make_client():
    return FakeSlackClient


@pytest.fixture
def folded():
    return folded_incident


@pytest.fixture
def standalone():
    return standalone_incident


@pytest.fixture
def fake_pool():
    return FakePool()


@pytest.fixture
def patched_db(fake_pool, monkeypatch):
    """Route every incidents write in the notification modules to fake_pool."""
    for module in (slack_notification_service, slack_threading, dispatcher):
        monkeypatch.setattr(module, "db_pool", fake_pool.pool)
        monkeypatch.setattr(module, "set_rls_context", lambda *a, **k: "org-1")
    return fake_pool


@pytest.fixture
def slack_helpers_stub(monkeypatch):
    """Stub routes.slack.slack_events_helpers; yields the stub so tests can
    flip validate_slack_blocks or assert on get_incident_suggestions."""
    stub = types.ModuleType("routes.slack.slack_events_helpers")
    stub.validate_slack_blocks = MagicMock(return_value=True)
    stub.extract_summary_section = lambda text: text
    stub.format_response_for_slack = lambda text: text
    stub.get_incident_suggestions = MagicMock(return_value=[])
    stub.build_suggestions_blocks = MagicMock(return_value=[])
    monkeypatch.setitem(sys.modules, "routes.slack.slack_events_helpers", stub)
    return stub

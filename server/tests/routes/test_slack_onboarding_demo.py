"""The demo trigger: "@Aurora demo" plays the canned incident, while a real
question that merely mentions a demo must still reach the agent."""

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def is_demo_request(monkeypatch):
    # The module registers a Celery task at import; the real celery_config dials Redis.
    celery_stub = types.ModuleType("celery_config")
    celery_stub.celery_app = MagicMock()
    celery_stub.celery_app.task.return_value = lambda fn: fn
    monkeypatch.setitem(sys.modules, "celery_config", celery_stub)
    name = "routes.slack.slack_onboarding"
    previous = sys.modules.pop(name, None)
    try:
        yield importlib.import_module(name).is_demo_request
    finally:
        # Don't leave the stub-task module behind for later imports.
        sys.modules.pop(name, None)
        if previous is not None:
            sys.modules[name] = previous


@pytest.mark.parametrize("message", [
    "demo", "Demo!", "show me a demo", "run a demo incident", "try the demo",
    "please show me a demo", "start demo rca",
])
def test_demo_requests_play_the_demo(is_demo_request, message):
    assert is_demo_request(message)


@pytest.mark.parametrize("message", [
    "why is the demo environment down?",
    "demo-api is returning 500s",
    "can you check the demo cluster",
    "",
])
def test_real_questions_are_not_hijacked(is_demo_request, message):
    assert not is_demo_request(message)

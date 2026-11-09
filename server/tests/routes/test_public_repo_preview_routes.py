"""Route contract for the low-friction public repository preview."""

from __future__ import annotations

import os
import sys
import types
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask

# Rate-limiter configuration is read when the route module is imported.  Tests
# replace the limiter with the shared pass-through stub, but the storage setting
# still has to exist during module import.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from routes import public_repo_preview_routes as routes
from services.public_repo_preview import PublicRepoPreviewError


@pytest.fixture
def app():
    return Flask(__name__)


@pytest.fixture
def route_env(monkeypatch):
    import chat
    import tasks

    create_session = MagicMock(return_value="session-123")
    enqueue = MagicMock()
    persist_failure = MagicMock()

    background_package = types.ModuleType("chat.background")
    background_task = types.ModuleType("chat.background.task")
    background_task.create_background_chat_session = create_session
    background_package.task = background_task
    monkeypatch.setitem(sys.modules, "chat.background", background_package)
    monkeypatch.setitem(sys.modules, "chat.background.task", background_task)
    monkeypatch.setattr(chat, "background", background_package, raising=False)

    preview_tasks = types.ModuleType("tasks.public_repo_preview_tasks")
    preview_tasks.run_public_repo_preview = SimpleNamespace(delay=enqueue)
    preview_tasks.persist_preview_failure = persist_failure
    monkeypatch.setitem(sys.modules, "tasks.public_repo_preview_tasks", preview_tasks)
    monkeypatch.setattr(tasks, "public_repo_preview_tasks", preview_tasks, raising=False)
    return SimpleNamespace(
        create_session=create_session,
        enqueue=enqueue,
        persist_failure=persist_failure,
    )


def test_preview_creates_visible_session_and_enqueues_canonical_url(
    app, route_env, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(routes, "parse_public_github_url", lambda _url: ("acme", "checkout"))

    with app.test_request_context(
        "/api/public-repo-preview",
        method="POST",
        json={"repository_url": "https://github.com/acme/checkout.git"},
    ):
        response, status = routes._start_public_repo_preview("user-1")

    assert status == 202
    assert response.get_json() == {
        "session_id": "session-123",
        "repository": "acme/checkout",
        "status": "in_progress",
    }
    session_kwargs = route_env.create_session.call_args.kwargs
    assert session_kwargs["user_id"] == "user-1"
    assert session_kwargs["title"] == "Reliability preview: acme/checkout"
    assert session_kwargs["question"] == (
        "Analyze public repository: https://github.com/acme/checkout"
    )
    assert session_kwargs["trigger_metadata"]["source"] == "public_repo_preview"
    assert session_kwargs["trigger_metadata"]["repository"] == "acme/checkout"
    assert session_kwargs["trigger_metadata"]["submitted_at"].endswith("+00:00")

    enqueue_kwargs = route_env.enqueue.call_args.kwargs
    assert enqueue_kwargs == {
        "user_id": "user-1",
        "session_id": "session-123",
        "repository_url": "https://github.com/acme/checkout",
        "repository": "acme/checkout",
        "submitted_at": session_kwargs["trigger_metadata"]["submitted_at"],
    }
    assert "preview=submitted" in caplog.text
    assert "acme/checkout" in caplog.text


@pytest.mark.parametrize(
    "body, expected_error",
    [
        (None, "Invalid JSON body"),
        ({}, "repository_url is required"),
        ({"repository_url": 7}, "repository_url is required"),
    ],
)
def test_preview_rejects_missing_or_invalid_body(
    app, route_env, body, expected_error
):
    kwargs = {"data": "not-json", "content_type": "application/json"} if body is None else {"json": body}
    with app.test_request_context(
        "/api/public-repo-preview", method="POST", **kwargs
    ):
        response, status = routes._start_public_repo_preview("user-1")

    assert status == 400
    assert response.get_json() == {"error": expected_error}
    route_env.create_session.assert_not_called()
    route_env.enqueue.assert_not_called()


def test_preview_returns_public_validation_message(app, route_env, monkeypatch):
    def reject(_url):
        raise PublicRepoPreviewError(400, "Use a canonical public GitHub URL.")

    monkeypatch.setattr(routes, "parse_public_github_url", reject)
    with app.test_request_context(
        "/api/public-repo-preview",
        method="POST",
        json={"repository_url": "https://example.com/private"},
    ):
        response, status = routes._start_public_repo_preview("user-1")

    assert status == 400
    assert response.get_json() == {"error": "Use a canonical public GitHub URL."}
    route_env.create_session.assert_not_called()


def test_enqueue_failure_marks_the_created_session_error(
    app, route_env, monkeypatch, caplog
):
    monkeypatch.setattr(routes, "parse_public_github_url", lambda _url: ("acme", "checkout"))
    route_env.enqueue.side_effect = RuntimeError("broker secret should not be logged")

    with app.test_request_context(
        "/api/public-repo-preview",
        method="POST",
        json={"repository_url": "https://github.com/acme/checkout"},
    ):
        response, status = routes._start_public_repo_preview("user-1")

    assert status == 503
    assert response.get_json() == {"error": "Could not start the repository preview"}
    route_env.persist_failure.assert_called_once_with(
        "session-123",
        "user-1",
        "Aurora couldn't start this preview. Please try again.",
    )
    assert "preview=failed" in caplog.text
    assert "broker secret" not in caplog.text

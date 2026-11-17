"""Background public-repository preview orchestration and safe failures."""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.public_repo_preview import PublicRepoPreviewError


@pytest.fixture
def preview_tasks(monkeypatch):
    celery_stub = types.ModuleType("celery_config")
    celery_stub.celery_app = MagicMock()
    celery_stub.celery_app.task.return_value = lambda fn: fn
    monkeypatch.setitem(sys.modules, "celery_config", celery_stub)
    name = "tasks.public_repo_preview_tasks"
    previous = sys.modules.pop(name, None)
    module = importlib.import_module(name)
    try:
        yield module
    finally:
        sys.modules.pop(name, None)
        if previous is not None:
            sys.modules[name] = previous


@pytest.fixture
def agent_env(monkeypatch):
    import chat

    apply = MagicMock(return_value=SimpleNamespace(result={"status": "completed"}))
    background_package = types.ModuleType("chat.background")
    background_task = types.ModuleType("chat.background.task")
    background_task.run_background_chat = SimpleNamespace(apply=apply)
    background_package.task = background_task
    monkeypatch.setitem(sys.modules, "chat.background", background_package)
    monkeypatch.setitem(sys.modules, "chat.background.task", background_task)
    monkeypatch.setattr(chat, "background", background_package, raising=False)
    return apply


def _snapshot():
    return {
        "full_name": "acme/checkout",
        "html_url": "https://github.com/acme/checkout",
        "default_branch": "main",
        "revision": "a" * 40,
        "files": [{"path": "Dockerfile", "content": "FROM python:3.12", "size": 16}],
    }


def test_task_fetches_snapshot_and_runs_existing_agent_read_only(
    preview_tasks, agent_env, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    fetch = MagicMock(return_value=_snapshot())
    monkeypatch.setattr(preview_tasks, "fetch_public_repository", fetch)
    monkeypatch.setattr(preview_tasks, "build_reliability_preview_prompt", lambda _s: "PROMPT")
    monkeypatch.setattr(preview_tasks, "build_reliability_preview_rail_text", lambda _s: "RAIL")
    persist = MagicMock()
    monkeypatch.setattr(preview_tasks, "persist_preview_failure", persist)

    result = preview_tasks.run_public_repo_preview(
        "user-1",
        "session-123",
        "https://github.com/acme/checkout",
        "acme/checkout",
        "2026-11-08T20:00:00+00:00",
    )

    assert result == {
        "status": "completed",
        "session_id": "session-123",
        "repository": "acme/checkout",
    }
    fetch.assert_called_once_with("https://github.com/acme/checkout")
    assert agent_env.call_args.kwargs["kwargs"] == {
        "user_id": "user-1",
        "session_id": "session-123",
        "initial_message": "PROMPT",
        "trigger_metadata": {
            "source": "public_repo_preview",
            "initial_message_is_scaffold": True,
            "repository": "acme/checkout",
            "submitted_at": "2026-11-08T20:00:00+00:00",
            "revision": "a" * 40,
        },
        "send_notifications": False,
        "mode": "ask",
        "rail_text": "RAIL",
    }
    persist.assert_not_called()
    assert "preview=completed" in caplog.text


def test_public_fetch_failure_persists_only_the_safe_public_message(
    preview_tasks, agent_env, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    def fail(_url):
        raise PublicRepoPreviewError(404, "Public GitHub repository not found.")

    monkeypatch.setattr(preview_tasks, "fetch_public_repository", fail)
    persist = MagicMock()
    monkeypatch.setattr(preview_tasks, "persist_preview_failure", persist)

    result = preview_tasks.run_public_repo_preview(
        "user-1",
        "session-123",
        "https://github.com/acme/missing",
        "acme/missing",
        "2026-11-08T20:00:00+00:00",
    )

    assert result["status"] == "failed"
    persist.assert_called_once_with(
        "session-123", "user-1", "Public GitHub repository not found."
    )
    agent_env.assert_not_called()
    assert "preview=failed" in caplog.text


def test_unexpected_prepare_failure_never_leaks_exception_text(
    preview_tasks, agent_env, monkeypatch, caplog
):
    def fail(_url):
        raise RuntimeError("token=super-secret repository contents")

    monkeypatch.setattr(preview_tasks, "fetch_public_repository", fail)
    persist = MagicMock()
    monkeypatch.setattr(preview_tasks, "persist_preview_failure", persist)

    result = preview_tasks.run_public_repo_preview(
        "user-1",
        "session-123",
        "https://github.com/acme/checkout",
        "acme/checkout",
        "2026-11-08T20:00:00+00:00",
    )

    assert result["status"] == "failed"
    persist.assert_called_once_with(
        "session-123", "user-1", preview_tasks._GENERIC_FAILURE
    )
    agent_env.assert_not_called()
    assert "super-secret" not in caplog.text
    assert "repository contents" not in caplog.text


class _Cursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Connection:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_failure_persistence_appends_bot_message_and_marks_error(
    preview_tasks, monkeypatch
):
    import utils.auth.stateless_auth as stateless_auth
    import utils.db.connection_pool as connection_pool

    connection = _Connection()

    @contextlib.contextmanager
    def get_connection():
        yield connection

    monkeypatch.setattr(connection_pool.db_pool, "get_admin_connection", get_connection)
    monkeypatch.setattr(
        stateless_auth,
        "set_rls_context",
        lambda *_args, **_kwargs: "org-1",
    )

    preview_tasks.persist_preview_failure(
        "session-123", "user-1", "Safe message for the user."
    )

    assert connection.committed is True
    sql, params = connection.cursor_obj.calls[-1]
    assert "status = 'error'" in sql
    assert json.loads(params[0]) == [
        {"sender": "bot", "text": "Safe message for the user."}
    ]
    assert params[1:] == ("session-123", "user-1")

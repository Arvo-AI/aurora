"""Tests for tasks.change_gating_try (job-state handling of the preview task).

Celery is stubbed so the task body runs as a plain function; Redis is a dict.
"""

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest

from services.change_gating.try_review import TryReviewError


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    def get(self, key):
        return self.store.get(key)


@pytest.fixture
def task_module(monkeypatch):
    celery_config = types.ModuleType("celery_config")

    class _App:
        def task(self, **_kwargs):
            return lambda fn: fn  # bind=True task body is called with a dummy self

    celery_config.celery_app = _App()
    monkeypatch.setitem(sys.modules, "celery_config", celery_config)

    redis_client = types.ModuleType("utils.cache.redis_client")
    fake = _FakeRedis()
    redis_client.get_redis_client = lambda: fake
    monkeypatch.setitem(sys.modules, "utils.cache.redis_client", redis_client)

    sys.modules.pop("tasks.change_gating_try", None)
    module = importlib.import_module("tasks.change_gating_try")
    module._fake_redis = fake
    yield module
    sys.modules.pop("tasks.change_gating_try", None)


def _state(module, user="u1", job="j1"):
    import json

    return json.loads(module._fake_redis.store[module.job_key(user, job)])


def test_job_keys_are_scoped_per_user_with_a_ttl(task_module):
    task_module.write_job("alice", "j1", {"status": "queued"})
    assert task_module.read_job("alice", "j1") == {"status": "queued"}
    assert task_module.read_job("bob", "j1") is None  # no cross-user reads
    assert task_module._fake_redis.ttls[task_module.job_key("alice", "j1")] == 3600


def test_success_stores_the_result_and_session(task_module, monkeypatch):
    ctx = MagicMock(source={"kind": "diff", "files": 1}, repo_label="pasted diff")
    monkeypatch.setattr(task_module, "load_pasted_diff", lambda payload: ctx)

    def fake_run_preview(c, run_agent):
        return {"verdict": "SAFE", "summary": "ok", "findings": []}

    monkeypatch.setattr(task_module, "run_preview", fake_run_preview)
    out = task_module.run_try_review(None, "u1", "j1", "diff", "diff --git a/x b/x")
    assert out == {"status": "completed"}
    state = _state(task_module)
    assert state["status"] == "completed"
    assert state["result"]["verdict"] == "SAFE"
    assert "duration_seconds" in state["result"]


def test_user_facing_errors_keep_their_code_and_message(task_module, monkeypatch):
    def boom(payload):
        raise TryReviewError("not_found_or_private", "Paste the diff instead.")

    monkeypatch.setattr(task_module, "load_public_pr", boom)
    out = task_module.run_try_review(None, "u1", "j1", "url", "https://github.com/a/b/pull/1")
    assert out == {"status": "failed", "code": "not_found_or_private"}
    assert _state(task_module)["error"] == {
        "code": "not_found_or_private",
        "message": "Paste the diff instead.",
    }


def test_unexpected_errors_do_not_leak_internals(task_module, monkeypatch):
    def boom(payload):
        raise RuntimeError("postgres://admin:secret@db/internal")

    monkeypatch.setattr(task_module, "load_pasted_diff", boom)
    task_module.run_try_review(None, "u1", "j1", "diff", "diff --git a/x b/x")
    error = _state(task_module)["error"]
    assert error["code"] == "internal_error"
    assert "secret" not in error["message"] and "postgres" not in error["message"]

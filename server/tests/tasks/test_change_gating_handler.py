"""Filter-matrix tests for the change-gating webhook handler.

Pins the enqueue contract of ``_maybe_enqueue_change_gating`` (called from
``_handle_pull_request_event`` in ``tasks/github_webhook_tasks.py``):
``investigate_pr.delay`` fires ONLY when a ``pull_request`` delivery passes
the full filter chain — gated action, non-draft, default-branch base,
installation present + not suspended, repo enrolled, Redis dedupe won.
Every branch must still mark the delivery processed.

Also pins the background-mode self-block of the Spinnaker
``trigger_pipeline`` action (``spinnaker_rca_tool.py`` ~L210-218): the PR
review agent runs with ``is_background=True``, so mutating pipeline
triggers must be rejected even though the tool itself is registered.

DB, Redis and Celery ``delay`` are all mocked — no I/O.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tasks.github_webhook_tasks as webhook_tasks

_DELIVERY_ID = "d-0001"
_INSTALLATION_ID = 555
_USER_ID = "user-1"
_REPO = "acme/api"
_PR_NUMBER = 7
_HEAD_SHA = "abc123"


def _payload(
    *,
    action: str = "opened",
    draft: bool = False,
    base_ref: str = "main",
    default_branch: str = "main",
    with_installation: bool = True,
) -> dict:
    payload = {
        "action": action,
        "pull_request": {
            "number": _PR_NUMBER,
            "draft": draft,
            "state": "open",
            "title": "Tighten retry loop",
            "merged_at": None,
            "head": {"sha": _HEAD_SHA},
            "base": {"ref": base_ref, "sha": "base456"},
            "user": {"login": "octocat"},
        },
        "repository": {"full_name": _REPO, "default_branch": default_branch},
    }
    if with_installation:
        payload["installation"] = {"id": _INSTALLATION_ID}
    return payload


class _FakeCursor:
    """Routes fetchone/fetchall on the last executed SQL statement."""

    def __init__(self, state: dict):
        self._state = state
        self._last_sql = ""

    def execute(self, sql, params=None):
        self._last_sql = sql

    def fetchone(self):
        if "FROM github_installations" in self._last_sql:
            return ("2026-01-01",) if self._state["suspended"] else (None,)
        if "FROM connected_repos" in self._last_sql:
            return (1,) if self._state["enrolled"] else None
        return None

    def fetchall(self):
        if "FROM user_github_installations" in self._last_sql:
            return [(uid,) for uid in self._state["linked_users"]]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, state: dict):
        self._state = state

    def cursor(self):
        return _FakeCursor(self._state)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, state: dict):
        self._state = state

    def get_admin_connection(self):
        return _FakeConn(self._state)


@pytest.fixture
def gating_env(monkeypatch):
    """Wire fake DB pool, RLS, Redis, delivery-status and investigate_pr.delay."""
    state = {
        "suspended": False,
        "enrolled": True,
        "linked_users": [_USER_ID],
    }
    import utils.auth.stateless_auth as stateless_auth
    import utils.cache.redis_client as redis_client_mod
    import utils.db.connection_pool as connection_pool

    monkeypatch.setattr(connection_pool, "db_pool", _FakePool(state))
    monkeypatch.setattr(
        stateless_auth, "set_rls_context", lambda cur, conn, uid, **kw: "org-1"
    )

    redis_mock = MagicMock()
    redis_mock.set.return_value = True  # NX won → not a duplicate
    monkeypatch.setattr(redis_client_mod, "get_redis_client", lambda: redis_mock)

    investigate_pr = MagicMock()
    change_gating_stub = ModuleType("tasks.change_gating")
    change_gating_stub.investigate_pr = investigate_pr
    change_gating_stub.change_gating_keys = lambda repo, pr, sha: {
        "seen": f"change_gating:seen:{repo}:{pr}:{sha}",
        "run": f"change_gating:run:{repo}:{pr}:{sha}",
        "posted": f"change_gating:posted:{repo}:{pr}:{sha}",
        "verdict": f"change_gating:verdict:{repo}:{pr}:{sha}",
    }  # NOTE: mirror tasks.change_gating.change_gating_keys exactly.
    monkeypatch.setitem(sys.modules, "tasks.change_gating", change_gating_stub)

    update_status = MagicMock()
    monkeypatch.setattr(webhook_tasks, "_update_delivery_status", update_status)

    return SimpleNamespace(
        state=state,
        redis=redis_mock,
        investigate_pr=investigate_pr,
        update_status=update_status,
    )


_MATRIX = [
    # (case_id, payload_overrides, state_overrides, redis_nx_won, expect_enqueue)
    ("wrong_action", {"action": "closed"}, {}, True, False),
    ("draft", {"draft": True}, {}, True, False),
    ("non_default_base", {"base_ref": "develop"}, {}, True, False),
    ("missing_installation", {"with_installation": False}, {}, True, False),
    ("suspended", {}, {"suspended": True}, True, False),
    ("not_enrolled", {}, {"enrolled": False}, True, False),
    ("duplicate_delivery", {}, {}, False, False),
    ("happy_path", {}, {}, True, True),
]


class TestPullRequestChangeGatingFilterMatrix:
    @pytest.mark.parametrize(
        "case_id, payload_overrides, state_overrides, redis_nx_won, expect_enqueue",
        _MATRIX,
        ids=[case[0] for case in _MATRIX],
    )
    def test_enqueue_only_on_happy_path(
        self,
        gating_env,
        case_id,
        payload_overrides,
        state_overrides,
        redis_nx_won,
        expect_enqueue,
    ):
        gating_env.state.update(state_overrides)
        gating_env.redis.set.return_value = redis_nx_won
        payload = _payload(**payload_overrides)

        webhook_tasks._handle_pull_request_event(
            payload, payload["action"], _DELIVERY_ID
        )

        if expect_enqueue:
            gating_env.investigate_pr.delay.assert_called_once_with(
                user_id=_USER_ID,
                installation_id=_INSTALLATION_ID,
                repo_full_name=_REPO,
                pr_number=_PR_NUMBER,
                head_sha=_HEAD_SHA,
                action="opened",
                delivery_id=_DELIVERY_ID,
            )
        else:
            gating_env.investigate_pr.delay.assert_not_called()

        # The pre-existing audit behavior must survive every branch.
        gating_env.update_status.assert_called_once_with(
            _DELIVERY_ID, status="processed"
        )

    def test_happy_path_uses_nx_dedupe_key(self, gating_env):
        payload = _payload(action="synchronize")

        webhook_tasks._handle_pull_request_event(
            payload, "synchronize", _DELIVERY_ID
        )

        gating_env.redis.set.assert_called_once_with(
            f"change_gating:seen:{_REPO}:{_PR_NUMBER}:{_HEAD_SHA}",
            _DELIVERY_ID,
            nx=True,
            ex=86400,
        )
        gating_env.investigate_pr.delay.assert_called_once()

    def test_duplicate_delivery_skips_before_any_db_work(self, gating_env, monkeypatch):
        """Dedupe runs BEFORE suspension/enrollment queries: a duplicate
        must not pay the DB cost (and must not need the DB at all)."""
        gating_env.redis.set.return_value = False  # NX lost → duplicate
        import utils.db.connection_pool as connection_pool

        boom = MagicMock()
        boom.get_admin_connection.side_effect = AssertionError(
            "duplicate delivery must not open a DB connection"
        )
        monkeypatch.setattr(connection_pool, "db_pool", boom)

        webhook_tasks._handle_pull_request_event(
            _payload(), "opened", _DELIVERY_ID
        )

        gating_env.investigate_pr.delay.assert_not_called()
        boom.get_admin_connection.assert_not_called()

    def test_enqueue_failure_releases_dedupe_key_and_raises(self, gating_env):
        """A failed .delay() must free the seen-key (so the dispatcher's
        Celery retry is not swallowed as duplicate_delivery) and propagate."""
        gating_env.investigate_pr.delay.side_effect = RuntimeError("broker down")

        with pytest.raises(RuntimeError, match="broker down"):
            webhook_tasks._handle_pull_request_event(
                _payload(), "opened", _DELIVERY_ID
            )

        gating_env.redis.delete.assert_called_once_with(
            f"change_gating:seen:{_REPO}:{_PR_NUMBER}:{_HEAD_SHA}"
        )


class TestProgressComment:
    """The transient 'Aurora is reviewing…' comment is tracked in a local
    id and cleared in a finally on every exit — no cross-attempt state."""

    def test_change_gating_keys_has_no_progress_key(self):
        # The progress comment is local-only; it must NOT add a Redis key.
        from tasks.change_gating import change_gating_keys
        keys = change_gating_keys(_REPO, _PR_NUMBER, _HEAD_SHA)
        assert set(keys) == {"seen", "run", "posted", "verdict"}

    def test_post_returns_comment_id(self):
        from tasks.change_gating import _post_progress_comment

        adapter = MagicMock()
        adapter.post_issue_comment.return_value = {"id": 4242}
        cid = _post_progress_comment(adapter, _PR_NUMBER, "ctx")
        assert cid == 4242
        adapter.post_issue_comment.assert_called_once()
        assert adapter.post_issue_comment.call_args.args[0] == _PR_NUMBER

    def test_post_failure_is_swallowed(self):
        from tasks.change_gating import _post_progress_comment

        adapter = MagicMock()
        adapter.post_issue_comment.side_effect = RuntimeError("403")
        assert _post_progress_comment(adapter, _PR_NUMBER, "ctx") is None

    def test_post_missing_id_returns_none(self):
        from tasks.change_gating import _post_progress_comment

        adapter = MagicMock()
        adapter.post_issue_comment.return_value = {}  # no 'id'
        assert _post_progress_comment(adapter, _PR_NUMBER, "ctx") is None

    def test_clear_deletes_by_id(self):
        from tasks.change_gating import _clear_progress_comment

        adapter = MagicMock()
        _clear_progress_comment(adapter, 4242, "ctx")
        adapter.delete_issue_comment.assert_called_once_with(4242)

    def test_clear_noop_when_id_none(self):
        from tasks.change_gating import _clear_progress_comment

        adapter = MagicMock()
        _clear_progress_comment(adapter, None, "ctx")
        adapter.delete_issue_comment.assert_not_called()

    def test_clear_swallows_delete_failure(self):
        from tasks.change_gating import _clear_progress_comment

        adapter = MagicMock()
        adapter.delete_issue_comment.side_effect = RuntimeError("500")
        _clear_progress_comment(adapter, 4242, "ctx")  # must not raise


class TestSpinnakerTriggerPipelineBackgroundBlock:
    """trigger_pipeline must self-block when the agent runs in background mode."""

    def test_trigger_pipeline_rejected_in_background_mode(self, monkeypatch):
        pytest.importorskip("pydantic")

        # Stub the lazy in-function imports so no real chat/agent stack loads.
        command_gate_stub = ModuleType("utils.auth.command_gate")
        command_gate_stub._is_org_tool_permitted = lambda tool_name: False
        command_gate_stub.gate_action = MagicMock()
        monkeypatch.setitem(sys.modules, "utils.auth.command_gate", command_gate_stub)

        cloud_tools_stub = ModuleType("chat.backend.agent.tools.cloud_tools")
        cloud_tools_stub.get_state_context = lambda: SimpleNamespace(is_background=True)
        monkeypatch.setitem(
            sys.modules, "chat.backend.agent.tools.cloud_tools", cloud_tools_stub
        )

        from chat.backend.agent.tools.spinnaker_rca_tool import spinnaker_rca

        result = json.loads(
            spinnaker_rca(
                action="trigger_pipeline",
                application="myapp",
                pipeline_name="deploy-prod",
                user_id=_USER_ID,
            )
        )

        assert "error" in result
        assert "not available in background mode" in result["error"]

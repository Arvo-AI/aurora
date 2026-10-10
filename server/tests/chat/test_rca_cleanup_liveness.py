"""Issue #682: run the real cleanup and workflow entry point in isolation.

AST loading follows the existing terminal_exec_tool tests. External services
are replaced; heartbeat, workflow consumer and cleanup bodies are unchanged.
"""

import ast
import asyncio
import importlib.util
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


SERVER = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 10, 15, 17, 1)
INCIDENT_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "22222222-2222-2222-2222-222222222222"
TASK_ID = "live-rca-task"
USER_ID = "test-user"


def load_functions(path, names, namespace):
    """Load selected function bodies without importing the rest of the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in functions} == set(names)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)


class DatabaseDouble:
    """Stateful query responses: a real heartbeat changes the cleaner's read."""

    def __init__(self, clock):
        """Start with a stale running session and mocked database connections."""
        self.clock = clock
        self.session_updated_at = NOW - timedelta(seconds=181)
        self.incident_updated_at = self.session_updated_at
        self.session_status = "in_progress"
        self.incident_status = "running"
        self.running_tool = False
        self.running_subagent = False
        self.heartbeat_count = 0
        self.on_heartbeat = lambda: None
        self.queries = []
        self.rows = []
        self.cursor = MagicMock()
        self.cursor.execute.side_effect = self.execute
        self.cursor.fetchone.side_effect = lambda: self.rows.pop(0) if self.rows else None
        self.cursor.fetchall.side_effect = self.fetchall
        self.conn = MagicMock()
        self.conn.cursor.return_value.__enter__.return_value = self.cursor
        self.pool = MagicMock()
        self.pool.get_admin_connection.return_value.__enter__.return_value = self.conn

    def fetchall(self):
        """Return the pending query rows and clear them."""
        rows, self.rows = self.rows, []
        return rows

    def execute(self, query, params=()):
        """Apply heartbeat and cleanup queries to the in-memory session state."""
        sql = " ".join(query.split())
        self.queries.append(sql)
        self.rows = []
        self.cursor.rowcount = 0
        if "SET updated_at = GREATEST(updated_at, NOW())" in sql:
            assert params == (SESSION_ID, USER_ID)
            assert "user_id = %s" in sql and "status = 'in_progress'" in sql
            if self.session_status == "in_progress":
                self.session_updated_at = self.clock.now()
                self.heartbeat_count += 1
                self.cursor.rowcount = 1
                self.on_heartbeat()
        elif sql.startswith("SELECT DISTINCT id FROM users"):
            self.rows = [(USER_ID,)]
        elif sql.startswith("SELECT cs.id, i.id as incident_id"):
            assert self.session_updated_at >= params[0], "Fixture targets section 2"
        elif sql.startswith("SELECT i.id, i.rca_celery_task_id"):
            if self.incident_status == "running" and self.incident_updated_at < params[0]:
                self.rows = [(INCIDENT_ID, TASK_ID, SESSION_ID, self.session_updated_at)]
        elif sql.startswith("SELECT 1 FROM execution_steps"):
            self.rows = [(1,)] if self.running_tool else []
        elif sql.startswith("SELECT 1 FROM rca_findings"):
            self.rows = [(1,)] if self.running_subagent else []
        elif sql.startswith("UPDATE incidents SET aurora_status = 'error'"):
            self.incident_status = "error"
            self.incident_updated_at = self.clock.now()
            self.rows = [(INCIDENT_ID,)]
            self.cursor.rowcount = 1
        elif sql.startswith("UPDATE chat_sessions SET status = 'failed'"):
            if self.session_status == "in_progress":
                self.session_status = "failed"
                self.session_updated_at = self.clock.now()
                self.rows = [(SESSION_ID,)]
                self.cursor.rowcount = 1
        elif sql.startswith("UPDATE chat_sessions SET status = %s"):
            status, updated_at, session_id, terminal_statuses = params
            assert session_id == SESSION_ID
            if self.session_status not in terminal_statuses:
                self.session_status = status
                self.session_updated_at = updated_at
                self.cursor.rowcount = 1
        elif sql.startswith("SELECT status FROM chat_sessions"):
            self.rows = [(self.session_status,)]
        elif sql.startswith("SELECT messages FROM chat_sessions"):
            self.rows = [([],)]
        elif sql.startswith((
            "SELECT i.id, i.aurora_status", "UPDATE rca_findings rf",
            "SET myapp.current_org_id", "UPDATE action_runs",
        )):
            pass
        elif sql.startswith("SELECT DISTINCT org_id FROM users"):
            self.rows = [("test-org",)]
        else:
            raise AssertionError(f"Unexpected SQL in reproduction: {sql}")


@pytest.fixture
def harness(monkeypatch):
    """Load real workflow and cleanup code with a controlled clock and database."""
    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            """Return the time set by the test."""
            assert tz is None
            return cls.current

    db = DatabaseDouble(Clock)
    auth = ModuleType("utils.auth.stateless_auth")
    auth.set_rls_context = MagicMock(return_value="test-org")
    pool_module = ModuleType("utils.db.connection_pool")
    pool_module.db_pool = db.pool
    dispatcher = ModuleType("utils.notifications.dispatcher")
    dispatcher.notify_investigation_failed = MagicMock()
    for module in (auth, pool_module, dispatcher):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    # Avoid importing chat.background's heavy package initializer.
    name = "chat.background.heartbeat"
    spec = importlib.util.spec_from_file_location(name, SERVER / "chat/background/heartbeat.py")
    heartbeat = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, heartbeat)
    spec.loader.exec_module(heartbeat)
    # Minutes of virtual time; milliseconds between scheduled test pulses.
    monkeypatch.setattr(heartbeat, "HEARTBEAT_INTERVAL_SECONDS", 0.005)

    namespace = {
        "celery_app": SimpleNamespace(
            AsyncResult=MagicMock(return_value=SimpleNamespace(state="STARTED", result=None)),
            task=lambda **kwargs: lambda function: function,
        ),
        "datetime": Clock,
        "timedelta": timedelta,
        "Dict": Dict,
        "Any": Any,
        "logger": logging.getLogger(__name__),
        "db_pool": db.pool,
        "set_rls_context": auth.set_rls_context,
        "_record_rca_error": MagicMock(),
        "_propagate_suggestion_status": MagicMock(),
        "asyncio": asyncio,
        "time": time,
        "json": json,
        "websockets": SimpleNamespace(exceptions=SimpleNamespace(ConnectionClosed=ConnectionError)),
        "update_api_cost_cache_async": AsyncMock(),
        "get_cached_api_cost": lambda user_id: (True, 0.0),
        "_background_tasks": set(),
    }
    load_functions(SERVER / "chat/background/task.py", (
        "_is_task_dead", "_affected_users", "cleanup_stale_background_chats",
    ), namespace)
    load_functions(SERVER / "main_chatbot.py", ("process_workflow_async",), namespace)
    return SimpleNamespace(
        db=db, clock=Clock, code=namespace, heartbeat=heartbeat,
        notify=dispatcher.notify_investigation_failed, auth=auth,
    )


@pytest.mark.parametrize(
    "age_seconds,running_tool,running_subagent",
    [(179, False, False), (181, True, False), (181, False, True)],
    ids=["recent-activity", "tool-currently-running", "subagent-currently-running"],
)
def test_existing_liveness_signals_are_accepted(harness, age_seconds, running_tool, running_subagent):
    """Recent activity, running tools and running subagents each keep a task alive."""
    harness.db.running_tool = running_tool
    harness.db.running_subagent = running_subagent
    assert harness.code["_is_task_dead"](
        TASK_ID, NOW - timedelta(seconds=age_seconds), NOW - timedelta(minutes=3),
        cursor=harness.db.cursor, incident_id=INCIDENT_ID,
    ) is False


@pytest.mark.parametrize(
    "enabled,stream_tokens",
    [(False, False), (True, False), (True, True)],
    ids=["baseline-without-heartbeat", "silent-model-with-heartbeat", "short-stream-with-heartbeat"],
)
def test_real_workflow_and_cleanup_during_model_wait(harness, monkeypatch, enabled, stream_tokens):
    """Heartbeats prevent cleanup from failing a workflow while the model waits."""
    if not enabled:
        @asynccontextmanager
        async def no_heartbeat(*args):
            """Run the baseline without refreshing session activity."""
            yield
        monkeypatch.setattr(harness.heartbeat, "background_session_heartbeat", no_heartbeat)

    async def scenario():
        """Compare cleanup outcomes after the model waits beyond the stale cutoff."""
        # Start both A/B arms from the same just-recorded activity, then advance
        # exactly 181 seconds without a tool call or a content-save-sized chunk.
        harness.db.session_updated_at = NOW
        harness.db.incident_updated_at = NOW
        pulse_received = asyncio.Event()
        loop = asyncio.get_running_loop()
        harness.db.on_heartbeat = lambda: loop.call_soon_threadsafe(pulse_received.set)
        observations = {}

        class WaitingModel:
            async def stream(self, state):
                """Advance virtual time and run cleanup between optional short tokens."""
                if stream_tokens:
                    # Too short to trigger either existing content-save threshold.
                    yield "token", "查"
                harness.clock.current += timedelta(seconds=181)
                if enabled:
                    # Wait for a pulse at the NEW time, not just the initial pulse.
                    while harness.db.session_updated_at < harness.clock.current:
                        pulse_received.clear()
                        await asyncio.wait_for(pulse_received.wait(), timeout=2)
                observations["cleanup"] = harness.code["cleanup_stale_background_chats"]()
                observations["status_at_check"] = harness.db.session_status
                observations["notifications"] = harness.notify.call_count
                observations["continued"] = True
                if stream_tokens:
                    yield "token", "看"

        state = SimpleNamespace(session_id=SESSION_ID, is_background=True)
        websocket = SimpleNamespace(send=AsyncMock())
        await harness.code["process_workflow_async"](WaitingModel(), state, websocket, USER_ID)
        assert observations.get("continued"), "Workflow failed before the cleanup check"
        assert "error" not in observations["cleanup"], observations["cleanup"]
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "background-session-heartbeat"]
        assert observations["cleanup"]["dead_tasks"] == (0 if enabled else 1)
        assert observations["status_at_check"] == ("in_progress" if enabled else "failed")
        assert observations["notifications"] == (0 if enabled else 1)
        label = "短片段流式输出＋心跳" if stream_tokens else ("模型静默等待＋心跳" if enabled else "对照：关闭心跳")
        print("中文结果：" + json.dumps({
            "场景": label,
            "模型等待秒数": 181,
            "误判死亡次数": observations["cleanup"]["dead_tasks"],
            "失败通知次数": observations["notifications"],
            "检查时会话": "运行中" if observations["status_at_check"] == "in_progress" else "失败",
            "检查后任务继续执行": "是",
        }, ensure_ascii=False))

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_mode", ["complete", "error", "cancel", "timeout"])
def test_heartbeat_stops_when_workflow_exits(harness, exit_mode):
    """Every workflow exit stops the heartbeat and further session updates."""
    async def scenario():
        """Exercise the chosen exit and check that no heartbeat work remains."""
        entered = asyncio.Event()

        async def work():
            """Enter the heartbeat context, then finish, fail or wait for cancellation."""
            async with harness.heartbeat.background_session_heartbeat(SESSION_ID, USER_ID):
                entered.set()
                if exit_mode == "error":
                    raise RuntimeError("model request failed")
                if exit_mode in ("cancel", "timeout"):
                    await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await asyncio.wait_for(entered.wait(), timeout=2)
        if exit_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_mode == "timeout":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=0.02)
        elif exit_mode == "error":
            with pytest.raises(RuntimeError, match="model request failed"):
                await task
        else:
            await task
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "background-session-heartbeat"]
        writes_after_exit = harness.db.heartbeat_count
        await asyncio.sleep(0.02)
        assert harness.db.heartbeat_count == writes_after_exit

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_heartbeat_does_not_reopen_terminal_session(harness, status):
    """A heartbeat leaves terminal session status and activity time unchanged."""
    harness.db.session_status = status
    previous = harness.db.session_updated_at
    harness.heartbeat._touch_session(SESSION_ID, USER_ID)
    assert harness.db.session_status == status
    assert harness.db.session_updated_at == previous
    assert harness.db.heartbeat_count == 0


def test_stopped_worker_eventually_becomes_stale_again(harness):
    """Cleanup fails a session once its last heartbeat becomes stale."""
    harness.heartbeat._touch_session(SESSION_ID, USER_ID)
    harness.clock.current += timedelta(seconds=181)
    result = harness.code["cleanup_stale_background_chats"]()
    assert "error" not in result
    assert result["dead_tasks"] == 1
    assert harness.db.session_status == "failed"


def test_heartbeat_requires_valid_org_context(harness):
    """A missing organization context prevents heartbeat database writes."""
    harness.auth.set_rls_context.return_value = None
    harness.heartbeat._touch_session(SESSION_ID, USER_ID)
    assert harness.db.queries == []
    assert harness.db.heartbeat_count == 0


def test_database_error_does_not_abort_workflow(harness, monkeypatch):
    """A heartbeat database failure is logged without raising to the caller."""
    harness.db.pool.get_admin_connection.side_effect = RuntimeError("database unavailable")
    monkeypatch.setattr(harness.heartbeat.logger, "warning", MagicMock())
    harness.heartbeat._touch_session(SESSION_ID, USER_ID)
    harness.heartbeat.logger.warning.assert_called_once()

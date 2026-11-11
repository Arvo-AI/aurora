"""Follow-ups to the investigation heartbeat and stall watchdog.

* A Slack answer posted before a later phase (Jira filing) finishes must not be
  replaced with an error when that phase fails, nor by the cleanup sweep when
  the worker dies in it.
* A session put back to in_progress for a follow-up is queued, not dead: the
  last run's heartbeat must not make the sweep fail it after five minutes.
* Calls that enforce an agent-supplied timeout report it to the watchdog.
* Prediscovery reads the heartbeat and fails its own session on error.
* The watchdog never signals the main thread once the run has finished.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import utils.cache.redis_client as redis_client

# task.py refuses to import without Redis. Leave a live client alone.
if redis_client.get_redis_client() is None:
    redis_client.get_redis_client = lambda: MagicMock(name="redis")

import chat.background.task as task
from chat.background import liveness
from utils.progress_watch import ProgressTracker, reset_tracker, set_tracker

SERVER = Path(__file__).resolve().parents[2]
SESSION = "44444444-4444-4444-4444-444444444444"
USER = "user-1"
SLACK_META = {"source": "slack", "channel": "C1", "thread_ts": "1.0", "thinking_message_ts": "2.0"}


class _RecordingPool:
    def __init__(self):
        self.cursor = MagicMock()
        self.conn = MagicMock()
        self.conn.cursor.return_value.__enter__.return_value = self.cursor

    def get_admin_connection(self):
        cm = MagicMock()
        cm.__enter__.return_value = self.conn
        return cm


def _function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


# --- 1. a delivered answer stays delivered -------------------------------------------

@pytest.fixture
def chat_sends(monkeypatch):
    calls = []
    monkeypatch.setattr(task, "_send_response_to_slack", lambda *a, **k: calls.append(("slack", k)) or True)
    monkeypatch.setattr(task, "_send_response_to_google_chat", lambda *a, **k: calls.append(("gchat", k)) or True)
    monkeypatch.setattr(task, "_send_response_to_teams", lambda *a, **k: calls.append(("teams", k)) or True)
    return calls


def test_early_slack_send_is_recorded_in_process_and_on_the_session(monkeypatch, chat_sends):
    marked = []
    monkeypatch.setattr(task, "_mark_reply_delivered", lambda sid, uid: marked.append((sid, uid)))
    state = task.ChatReplyState()

    assert task._send_early_slack_reply(USER, SESSION, SLACK_META, state) is True

    assert state.sent is True
    assert marked == [(SESSION, USER)]


def test_a_failed_early_send_records_nothing(monkeypatch):
    marked = []
    monkeypatch.setattr(task, "_mark_reply_delivered", lambda sid, uid: marked.append(sid))
    monkeypatch.setattr(task, "_send_response_to_slack", lambda *a, **k: False)
    state = task.ChatReplyState()

    assert task._send_early_slack_reply(USER, SESSION, SLACK_META, state) is False
    assert state.sent is False and marked == []


def test_no_early_send_outside_slack(chat_sends):
    state = task.ChatReplyState()
    for meta in ({"source": "teams"}, {"source": "google_chat"}, {"source": "grafana"}, None):
        assert task._send_early_slack_reply(USER, SESSION, meta, state) is False
    assert chat_sends == [] and state.sent is False


def test_a_failure_after_the_answer_was_sent_does_not_replace_it(chat_sends):
    """The Jira phase stalled after the answer went out: the run failed, and the
    finally block used to force the error text over the delivered answer."""
    for meta in (SLACK_META, {"source": "google_chat"}, {"source": "teams"}):
        task._resolve_chat_placeholder(
            USER, SESSION, meta, completed_successfully=False, reply_sent=True,
        )
    assert chat_sends == []


def test_a_failure_before_any_answer_still_resolves_thinking(chat_sends):
    task._resolve_chat_placeholder(
        USER, SESSION, SLACK_META, completed_successfully=False, reply_sent=False,
    )
    assert chat_sends == [("slack", {"force_text": task._CHAT_ERROR_FALLBACK})]


def test_the_task_wires_the_reply_state_through_the_workflow_and_the_finally():
    """Where Celery is stubbed the task decorator hides run_background_chat, so
    its wiring is read from the source."""
    func = _function(SERVER / "chat/background/task.py", "run_background_chat")
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)]

    workflow = next(c for c in calls if getattr(c.func, "id", None) == "_execute_background_chat")
    reply_kw = {k.arg: k.value for k in workflow.keywords}["reply_state"]
    assert reply_kw.id == "_reply_state"

    resolve = next(c for c in calls if getattr(c.func, "id", None) == "_resolve_chat_placeholder")
    reply_sent = {k.arg: k.value for k in resolve.keywords}["reply_sent"]
    names = {n.value.id for n in ast.walk(reply_sent) if isinstance(n, ast.Attribute)}
    assert "_reply_state" in names


def test_the_workflow_sends_early_through_the_recording_helper():
    func = _function(SERVER / "chat/background/task.py", "_execute_background_chat")
    called = {getattr(n.func, "id", None) for n in ast.walk(func) if isinstance(n, ast.Call)}
    assert "_send_early_slack_reply" in called
    assert "_send_response_to_slack" not in called


def test_sweep_leaves_an_answered_thread_alone(chat_sends):
    """The worker died in the Jira phase, after the answer was posted."""
    for source in ("slack", "google_chat", "teams"):
        task._notify_orphaned_chat_reply(USER, SESSION, {
            "triggerMetadata": {"source": source, "replied": True},
        })
    assert chat_sends == []


def test_sweep_still_replaces_thinking_when_nothing_was_answered(chat_sends):
    task._notify_orphaned_chat_reply(USER, SESSION, json.dumps({
        "triggerMetadata": {**SLACK_META, "replied": False},
    }))
    assert chat_sends == [("slack", {"force_text": task._WORKER_LOST_REPLY})]


def test_marking_the_reply_merges_into_the_trigger_metadata(monkeypatch):
    pool = _RecordingPool()
    monkeypatch.setattr(task, "db_pool", pool)
    monkeypatch.setattr(task, "set_rls_context", lambda *a, **k: "org-1")

    task._mark_reply_delivered(SESSION, USER)

    sql, (payload, session_id) = pool.cursor.execute.call_args.args
    assert sql == task._REMEMBER_REPLY_TARGET_SQL
    assert json.loads(payload) == {"replied": True}
    assert session_id == SESSION
    pool.conn.commit.assert_called_once()


def test_a_new_run_starts_unanswered(monkeypatch):
    """A follow-up on a session an earlier run answered must not inherit its flag."""
    pool = _RecordingPool()
    monkeypatch.setattr(task, "db_pool", pool)
    monkeypatch.setattr(task, "set_rls_context", lambda *a, **k: "org-1")

    task._remember_reply_target(SESSION, USER, SLACK_META)

    _sql, (payload, _sid) = pool.cursor.execute.call_args.args
    assert json.loads(payload) == {**SLACK_META, "replied": False}


# --- 2. re-armed sessions are queued, not dead -----------------------------------------

def _rearming_updates():
    """Every execute() in the server that sets a chat session back to in_progress."""
    found = []
    for path in SERVER.rglob("*.py"):
        if "tests" in path.relative_to(SERVER).parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "execute" and node.args):
                continue
            sql = node.args[0]
            if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                continue
            text = " ".join(sql.value.split())
            if "UPDATE chat_sessions" not in text:
                continue
            set_clause = text.split(" SET ", 1)[-1].split(" WHERE ", 1)[0]
            params = node.args[1] if len(node.args) > 1 else None
            by_literal = "status = 'in_progress'" in set_clause
            by_param = (
                "status = %s" in set_clause
                and isinstance(params, ast.Tuple)
                and any(isinstance(e, ast.Constant) and e.value == "in_progress" for e in params.elts)
            )
            if by_literal or by_param:
                found.append((f"{path.relative_to(SERVER)}:{node.lineno}", set_clause))
    return found


def test_every_rearm_clears_the_last_runs_heartbeat():
    updates = _rearming_updates()
    files = {where.split(":")[0] for where, _ in updates}
    assert {"routes/chat_routes.py", "routes/incidents_routes.py"} <= files
    for where, set_clause in updates:
        assert "heartbeat_at = NULL" in set_clause, where
        assert "updated_at" in set_clause, where


def test_a_rearmed_session_gets_the_queue_allowance():
    """With heartbeat_at NULL the sweep waits the never-started 20 minutes."""
    assert "heartbeat_at IS NULL AND updated_at < %s" in task._WORKER_GONE_SQL
    assert task._NEVER_STARTED_MINUTES > task._DEAD_WORKER_MINUTES


# --- 3. long calls with their own timeout are bounded ----------------------------------

@pytest.fixture
def tracker():
    t = ProgressTracker(600)
    token = set_tracker(t)
    try:
        yield t
    finally:
        reset_tracker(token)


def test_tailscale_local_ssh_reports_its_timeout(monkeypatch, tracker):
    from chat.backend.agent.tools import tailscale_ssh_tool

    seen = {}

    def _run(cmd, **kwargs):
        seen["bounds"] = list(tracker._bounds.values())
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", _run)
    tailscale_ssh_tool._execute_ssh_local("host-1", "uptime --secret", "root", 1800, None)

    (started, deadline, label), = seen["bounds"]
    assert deadline - started == 1800 + 120
    assert label == "command 'ssh'"
    assert tracker._bounds == {}


def test_onprem_kubectl_reports_its_timeout(monkeypatch, tracker):
    from chat.backend.agent.tools import kubectl_onprem_tool

    seen = {}

    class _Resp:
        ok = True
        status_code = 200

        def json(self):
            return {"success": True, "output": "ok", "return_code": 0}

    def _post(*_a, **_k):
        seen["bounds"] = list(tracker._bounds.values())
        return _Resp()

    monkeypatch.setenv("CHATBOT_INTERNAL_URL", "http://chatbot:5006")
    monkeypatch.setattr(kubectl_onprem_tool.requests, "post", _post)
    monkeypatch.setattr(
        "utils.auth.command_gate.gate_command",
        lambda **_k: types.SimpleNamespace(allowed=True),
    )

    kubectl_onprem_tool.on_prem_kubectl("get pods -A", cluster_id="c1", user_id=USER, timeout=1500)

    assert "bounds" in seen, "the request was never sent"
    (started, deadline, label), = seen["bounds"]
    assert deadline - started == 1500 + 120
    assert label == "command 'kubectl'"
    assert tracker._bounds == {}


# --- 4a. prediscovery -----------------------------------------------------------------

@pytest.fixture
def run_status(monkeypatch):
    flask = pytest.importorskip("flask")
    import routes.prediscovery.routes as routes

    def _run(row):
        pool = _RecordingPool()
        pool.cursor.fetchone.return_value = row
        monkeypatch.setattr(routes, "db_pool", pool)
        monkeypatch.setattr(routes, "set_rls_context", lambda *a, **k: "org-1")
        monkeypatch.setattr(routes, "get_org_id_from_request", lambda: "org-1")
        handler = routes.get_prediscovery_status
        while hasattr(handler, "__wrapped__"):
            handler = handler.__wrapped__
        with flask.Flask(__name__).test_request_context("/status"):
            response = handler(USER)
        return response.get_json(), pool.cursor.execute.call_args.args[0]

    return _run


def test_a_beating_prediscovery_is_running_however_old_its_updated_at(run_status):
    now = datetime.now()
    row = (SESSION, "in_progress", now - timedelta(hours=1), now - timedelta(minutes=50), now - timedelta(seconds=20))
    body, sql = run_status(row)
    assert "GREATEST(heartbeat_at, updated_at)" in sql
    assert body["status"] == "in_progress"


def test_a_prediscovery_that_stopped_beating_reads_as_failed(run_status):
    now = datetime.now()
    row = (SESSION, "in_progress", now - timedelta(hours=1), now - timedelta(minutes=50), now - timedelta(minutes=40))
    body, _ = run_status(row)
    assert body["status"] == "failed"


@pytest.fixture
def prediscovery(monkeypatch):
    import celery.exceptions  # noqa: F401  (registers the module, real or stubbed)
    from chat.background import prediscovery_task

    class _SoftTimeLimitExceeded(Exception):
        pass

    # Where Celery is stubbed the exception is a MagicMock, which except cannot catch.
    monkeypatch.setattr(sys.modules["celery.exceptions"], "SoftTimeLimitExceeded",
                        _SoftTimeLimitExceeded, raising=False)
    statuses = []
    monkeypatch.setattr(task, "_get_connected_integrations", lambda _uid: {"datadog": True})
    monkeypatch.setattr(task, "create_background_chat_session", lambda **_k: SESSION)
    monkeypatch.setattr(task, "_update_session_status",
                        lambda sid, status, user_id: statuses.append((sid, status, user_id)))

    async def _never_awaited(**_k):
        return {}

    monkeypatch.setattr(task, "_execute_background_chat", _never_awaited)
    monkeypatch.setattr("chat.background.rca_prompt_builder.get_user_providers", lambda _uid: ["aws"])
    monkeypatch.setattr("utils.auth.stateless_auth.get_org_id_for_user", lambda _uid: "org-1")
    monkeypatch.setattr("utils.hooks.get_hook", lambda _name: (lambda *_a: (True, None)))
    monkeypatch.setattr(prediscovery_task, "build_prediscovery_prompt", lambda *a: "discover")

    def _with_run(run_watched):
        def _close_and_run(coro, *a, **k):
            coro.close()
            return run_watched()
        monkeypatch.setattr(liveness, "run_watched", _close_and_run)
        return prediscovery_task._run_prediscovery("task-1", USER, "manual")

    return types.SimpleNamespace(run=_with_run, statuses=statuses, soft=_SoftTimeLimitExceeded)


def test_a_failed_prediscovery_fails_its_session(prediscovery):
    def _boom():
        raise liveness.InvestigationStalled("Investigation stalled: no progress for 600s")

    result = prediscovery.run(_boom)

    assert result["status"] == "error"
    assert prediscovery.statuses == [(SESSION, "failed", USER)]


def test_a_timed_out_prediscovery_fails_its_session(prediscovery):
    def _soft():
        raise prediscovery.soft()

    result = prediscovery.run(_soft)

    assert result["status"] == "timeout"
    assert prediscovery.statuses == [(SESSION, "failed", USER)]


def test_a_finished_prediscovery_completes_its_session(prediscovery):
    result = prediscovery.run(lambda: {})
    assert result["status"] == "completed"
    assert prediscovery.statuses == [(SESSION, "completed", USER)]


# --- 4b. the watchdog never signals a finished run ---------------------------------------

def test_no_interrupt_once_the_run_has_finished(monkeypatch):
    """The watchdog passed its stopping check while the main thread was leaving
    asyncio.run. The soft-limit signal then landed outside run_watched's error
    conversion and replaced the real result."""
    kills = []
    monkeypatch.setattr(liveness, "_interrupt_main_thread", lambda: kills.append(1) or True)
    watchdog = liveness._Watchdog(ProgressTracker(0.01))

    watchdog.mark_finished()

    assert watchdog._interrupt_if_running() is False
    assert kills == []


def test_finishing_waits_for_an_interrupt_already_in_flight(monkeypatch):
    """Checking and signalling happen under one lock, so the main thread cannot
    mark the run finished between the watchdog's check and its signal."""
    in_kill = threading.Event()
    release = threading.Event()

    def _slow_interrupt():
        in_kill.set()
        release.wait(2)
        return True

    monkeypatch.setattr(liveness, "_interrupt_main_thread", _slow_interrupt)
    watchdog = liveness._Watchdog(ProgressTracker(0.01))
    signaller = threading.Thread(target=watchdog._interrupt_if_running)
    signaller.start()
    assert in_kill.wait(2)

    finisher = threading.Thread(target=watchdog.mark_finished)
    finisher.start()
    finisher.join(0.1)
    assert finisher.is_alive(), "finished while a signal was being sent"

    release.set()
    signaller.join(2)
    finisher.join(2)
    assert watchdog._finished is True


def test_run_watched_marks_the_run_finished_before_returning(monkeypatch):
    seen = []
    real_mark = liveness._Watchdog.mark_finished

    def _mark(self):
        seen.append("finished")
        real_mark(self)

    monkeypatch.setattr(liveness._Watchdog, "mark_finished", _mark)
    monkeypatch.setattr(liveness, "touch_session", lambda *_a: True)

    async def _ok():
        return "answer"

    async def _fails():
        raise ValueError("boom")

    assert liveness.run_watched(_ok(), SESSION, USER, interval=0.01, idle_seconds=60) == "answer"
    with pytest.raises(ValueError):
        liveness.run_watched(_fails(), SESSION, USER, interval=0.01, idle_seconds=60)
    assert seen == ["finished", "finished"]


def test_a_watchdog_that_gives_up_after_the_run_finished_does_not_signal(monkeypatch):
    clock = [0.0]
    kills = []
    monkeypatch.setattr(liveness, "_interrupt_main_thread", lambda: kills.append(1) or True)
    tracker = ProgressTracker(1, clock=lambda: clock[0])
    watchdog = liveness._Watchdog(tracker, clock=lambda: clock[0])
    stopping = threading.Event()

    clock[0] = 5
    watchdog.check(stopping)  # stalls and queues the cancel
    watchdog.mark_finished()  # the run ended on its own meanwhile
    clock[0] = 5 + liveness.CANCEL_GRACE_SECONDS + 1
    watchdog.check(stopping)

    assert watchdog.gave_up is True
    assert kills == []

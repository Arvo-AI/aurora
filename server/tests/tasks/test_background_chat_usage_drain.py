"""End of a background RCA: queued usage rows are drained before the loop goes away.

``_execute_background_chat`` runs under ``asyncio.run()``. The input rail and the memory
selector hand their ``llm_usage_tracking`` rows to the process-owned usage writer thread,
so the loop's teardown cannot cancel them; the ``finally`` of the run additionally waits
(bounded) for what is still queued, before it stops the save queue, so a worker that
exits during event-loop teardown cannot take the rows with it (review on #740, round 2).

The run is driven to its ``finally`` by making the first step fail: the heavy modules the
function imports at its top (agent, workflow, ``main_chatbot``) are replaced by stubs,
because none of them can be imported without the full service environment.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def task_module():
    """chat.background.task, imported with the Redis client stubbed (it builds one at import)."""
    import utils.cache.redis_client as redis_client

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redis_client, "get_redis_client", MagicMock)
        mp.delitem(sys.modules, "chat.background.task", raising=False)
        import chat.background.task as task

        yield task

    # The import above cached a Redis-stubbed module; drop it if it's still ours.
    if sys.modules.get("chat.background.task") is task:
        sys.modules.pop("chat.background.task", None)


_STUBBED_IMPORTS = {
    "chat.backend.agent.agent": ("Agent",),
    "chat.backend.agent.db": ("PostgreSQLClient",),
    "chat.backend.agent.utils.state": ("State",),
    "chat.backend.agent.workflow": ("Workflow",),
    "chat.backend.agent.tools.cloud_tools": ("set_user_context",),
    "main_chatbot": ("process_workflow_async",),
}


@pytest.fixture
def run_events(task_module, monkeypatch):
    """Stub the function-level imports and the first step; returns the ordered event log."""
    events = []

    for module_name, attributes in _STUBBED_IMPORTS.items():
        stub = types.ModuleType(module_name)
        for attribute in attributes:
            setattr(stub, attribute, MagicMock(name=attribute))
        monkeypatch.setitem(sys.modules, module_name, stub)

    class _SaveQueue:
        async def stop(self):
            events.append("save_queue_stop")

    class _ContextManager:
        _instance = types.SimpleNamespace(async_queue=_SaveQueue())

    context_manager = types.ModuleType("chat.backend.agent.utils.persistence.context_manager")
    context_manager.ContextManager = _ContextManager
    monkeypatch.setitem(sys.modules, context_manager.__name__, context_manager)

    def _first_step_fails():
        raise RuntimeError("worker agent unavailable")

    monkeypatch.setattr(task_module, "_get_worker_agent", _first_step_fails)
    return events


def _run_background_chat(task_module):
    return asyncio.run(
        task_module._execute_background_chat(
            user_id="u-1", session_id="s-1", initial_message="why is checkout slow?"
        )
    )


def test_the_run_drains_the_usage_writer_before_stopping_the_save_queue(task_module, run_events, monkeypatch):
    drain_threads = []

    def _drain(timeout=5.0):
        run_events.append(("drain", timeout))
        drain_threads.append(threading.current_thread())
        return True

    monkeypatch.setattr(task_module.usage_writer, "drain", _drain)

    with pytest.raises(RuntimeError, match="worker agent unavailable"):
        _run_background_chat(task_module)

    assert run_events == [("drain", task_module._USAGE_DRAIN_TIMEOUT_S), "save_queue_stop"]
    # The bounded wait must not block the event loop thread.
    assert drain_threads[0] is not threading.current_thread()


@pytest.mark.parametrize("drain_outcome", ["raises", "times_out"])
def test_a_drain_problem_never_masks_the_run_or_skips_the_save_queue_cleanup(
    task_module, run_events, monkeypatch, caplog, drain_outcome
):
    def _drain(timeout=5.0):
        run_events.append("drain")
        if drain_outcome == "raises":
            raise OSError("usage writer unavailable")
        return False

    monkeypatch.setattr(task_module.usage_writer, "drain", _drain)

    with caplog.at_level("WARNING", logger=task_module.logger.name):
        # The run's own error is what surfaces, never the drain's.
        with pytest.raises(RuntimeError, match="worker agent unavailable"):
            _run_background_chat(task_module)

    assert run_events == ["drain", "save_queue_stop"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("usage write" in message.lower() for message in warnings), warnings


def test_a_real_queued_write_is_stored_before_the_run_returns(task_module, run_events):
    """No patching of ``drain``: a write held open while the workflow fails is complete by
    the time ``asyncio.run`` returns, because the run waited for the writer thread."""
    from utils import usage_writer

    release = threading.Event()
    started = threading.Event()
    written = []

    def _slow_write():
        started.set()
        release.wait(timeout=5)
        written.append("row")

    usage_writer.submit(_slow_write)
    assert started.wait(timeout=5)

    # Release the write only once the run has reached its drain (the drain blocks a
    # worker thread, not the loop, so a timer on the loop can still fire).
    async def _release_soon():
        await asyncio.sleep(0)
        release.set()

    original = task_module._drain_usage_writes

    async def _drain_and_release():
        asyncio.get_running_loop().create_task(_release_soon())
        await original()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(task_module, "_drain_usage_writes", _drain_and_release)
        with pytest.raises(RuntimeError, match="worker agent unavailable"):
            _run_background_chat(task_module)

    # Observed before any further drain: the run itself waited for the row.
    assert written == ["row"]
    assert run_events == ["save_queue_stop"]

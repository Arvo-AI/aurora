import asyncio
import sys
import types


def test_failed_action_is_reported_as_error(monkeypatch):
    """Reproduce success being recorded after the action workflow failed."""
    from chat.background import task

    status_updates = []
    notifications = []

    class FakeState:
        guardrail_blocked = False

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            # This is what Workflow records when a tool returns an error.
            self.last_tool_failure = {
                "tool_name": "cloud_exec",
                "message": "permission denied",
            }

    class FakeWorkflow:
        def __init__(self, agent, session_id):
            self._wait_for_ongoing_tool_calls = lambda: asyncio.sleep(0)

    class FakeWebSocket:
        def __init__(self, on_message=None):
            self.on_message = on_message

    async def fake_process_workflow(*args, **kwargs):
        # The workflow has completed, but its tool work failed.
        return None

    def install_module(name, **attributes):
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    install_module("chat.backend.agent.agent", Agent=object)
    install_module("chat.backend.agent.db", PostgreSQLClient=object)
    install_module("chat.backend.agent.utils.state", State=FakeState)
    install_module("chat.backend.agent.workflow", Workflow=FakeWorkflow)
    install_module(
        "chat.backend.agent.tools.cloud_tools",
        set_user_context=lambda **kwargs: None,
    )
    install_module(
        "chat.background.background_websocket",
        BackgroundWebSocket=FakeWebSocket,
    )
    install_module("main_chatbot", process_workflow_async=fake_process_workflow)
    install_module(
        "chat.backend.agent.utils.persistence.context_manager",
        ContextManager=type("ContextManager", (), {"_instance": None}),
    )

    monkeypatch.setattr(task, "_get_worker_agent", lambda: object())
    monkeypatch.setattr(task, "_build_rca_context", lambda **kwargs: None)
    monkeypatch.setattr(task, "_resolve_permitted_tools", lambda user_id: None)
    monkeypatch.setattr(task, "_action_is_generate_postmortem", lambda *args: False)
    monkeypatch.setattr(task, "_action_has_system_key", lambda *args: False)
    monkeypatch.setattr(task, "_should_file_in_jira", lambda *args: False)
    monkeypatch.setattr(task, "_update_session_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(task, "_ensure_llm_context_history", lambda *args: [])
    monkeypatch.setattr(task, "_extract_tool_calls_for_viz", lambda *args: [])

    monkeypatch.setattr(
        "services.actions.executor.update_action_run_status",
        lambda **kwargs: status_updates.append(kwargs),
    )
    monkeypatch.setattr(
        "utils.notifications.dispatcher.notify_action_completed",
        lambda *args, **kwargs: notifications.append(kwargs),
    )

    asyncio.run(
        task._execute_background_chat(
            user_id="user-1",
            session_id="session-1",
            initial_message="Run the action",
            trigger_metadata={"source": "action", "run_id": "run-1"},
            send_notifications=False,
        )
    )

    assert status_updates == [
        {
            "run_id": "run-1",
            "status": "error",
            "user_id": "user-1",
            "error_message": "permission denied",
        }
    ]
    assert notifications == [
        {"status": "error", "error_message": "permission denied"}
    ]

"""Local HTTP fault injection after the first streamed model output.

Execute the production workflow consumer, background executor and task entry
point. Provider construction, database and Celery services are isolated. The
HTTP disconnect/read timeout is real, using only a localhost test server.
"""

import asyncio
import json
import sys
from contextlib import suppress
from types import ModuleType, SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.chat.test_rca_cleanup_liveness import (
    SERVER, SESSION_ID, TASK_ID, USER_ID, harness, load_functions,
)


@pytest.mark.parametrize("fault", ["read_timeout", "disconnect", "silent_until_workflow_timeout"])
def test_network_failure_after_first_chunk_is_not_success(harness, monkeypatch, fault):
    """A stalled or disconnected stream fails the session after its first token."""
    observed = {"first_chunks": 0, "network_error": None, "workflow_limit": None}
    websocket = SimpleNamespace(send=AsyncMock())

    class NetworkWorkflow:
        def __init__(self, agent, session_id):
            """Keep the agent supplied by the background executor."""
            self.agent = agent

        async def stream(self, state):
            """Read tokens from a local server that stalls or disconnects midstream."""
            first_chunk_received = asyncio.Event()
            connections = set()
            handlers = set()

            async def serve(reader, writer):
                """Send one HTTP chunk, then trigger the selected network fault."""
                handler = asyncio.current_task()
                handlers.add(handler)
                connections.add(writer)
                try:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                    )
                    payload = "data: 正在分析\n\n".encode("utf-8")
                    writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                    await writer.drain()
                    await first_chunk_received.wait()
                    if fault == "disconnect":
                        # Close without the terminating HTTP chunk: incomplete response.
                        writer.close()
                    else:
                        # Keep the socket open but never send another byte.
                        await reader.read()
                finally:
                    writer.close()
                    with suppress(ConnectionError):
                        await writer.wait_closed()
                    connections.discard(writer)
                    handlers.discard(handler)

            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            read_timeout = None if fault == "silent_until_workflow_timeout" else 0.1
            try:
                async with httpx.AsyncClient(
                    trust_env=False,
                    timeout=httpx.Timeout(2.0, read=read_timeout),
                ) as client:
                    async with client.stream("GET", f"http://127.0.0.1:{port}/model") as response:
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                observed["first_chunks"] += 1
                                yield "token", line[6:]
                                first_chunk_received.set()
            except httpx.TransportError as error:
                observed["network_error"] = type(error).__name__
                raise
            finally:
                server.close()
                await server.wait_closed()
                for writer in list(connections):
                    writer.close()
                for handler in list(handlers):
                    handler.cancel()
                await asyncio.gather(*list(handlers), return_exceptions=True)

    class TestAsyncio:
        def __getattr__(self, name):
            """Forward unchanged operations to the real asyncio module."""
            return getattr(asyncio, name)

        async def wait_for(self, awaitable, timeout):
            """Record the production workflow limit and shorten it for the test."""
            if timeout == 1800:
                # Preserve and record the production limit, accelerate only this test.
                observed["workflow_limit"] = timeout
                timeout = 1.0
            return await asyncio.wait_for(awaitable, timeout=timeout)

    async def observe_async_exit():
        """Record whether the heartbeat has stopped before queue shutdown."""
        observed["heartbeat_stopped"] = not any(
            task.get_name() == "background-session-heartbeat"
            for task in asyncio.all_tasks()
        )

    modules = {
        "celery.exceptions": {
            "SoftTimeLimitExceeded": type("SoftTimeLimitExceeded", (Exception,), {}),
        },
        "celery_config": {"_prewarm_ready": SimpleNamespace(wait=lambda **kwargs: True)},
        "utils.hooks": {"get_hook": lambda name: lambda *args: (True, None)},
        "chat.backend.agent.agent": {"Agent": SimpleNamespace},
        "chat.backend.agent.db": {"PostgreSQLClient": SimpleNamespace},
        "chat.backend.agent.utils.state": {"State": SimpleNamespace},
        "chat.backend.agent.workflow": {"Workflow": NetworkWorkflow},
        "chat.backend.agent.tools.cloud_tools": {"set_user_context": MagicMock()},
        "chat.background.background_websocket": {"BackgroundWebSocket": lambda: websocket},
        "chat.backend.agent.llm": {"ModelConfig": SimpleNamespace(RCA_MODEL="local-fault-injection")},
        "chat.backend.agent.utils.persistence.context_manager": {
            "ContextManager": SimpleNamespace(_instance=SimpleNamespace(
                async_queue=SimpleNamespace(stop=observe_async_exit),
            )),
        },
        "main_chatbot": {"process_workflow_async": harness.code["process_workflow_async"]},
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    harness.auth.get_org_id_for_user = lambda user_id: "test-org"
    harness.code["celery_app"].backend = SimpleNamespace(client=MagicMock())
    harness.code.update({
        "asyncio": TestAsyncio(),
        "Optional": Optional,
        "List": List,
        "HumanMessage": SimpleNamespace,
        "os": SimpleNamespace(environ={}, getenv=lambda name, default=None: default),
        "_RCA_SOURCES": set(),
        "TERMINAL_SESSION_STATUSES": frozenset({"completed", "failed", "cancelled"}),
        "_get_worker_agent": lambda: SimpleNamespace(),
        "_build_rca_context": lambda **kwargs: None,
        "_resolve_permitted_tools": lambda user_id: None,
        "_should_file_in_jira": lambda *args: False,
        "_ensure_llm_context_history": MagicMock(return_value=[]),
        "_extract_tool_calls_for_viz": MagicMock(return_value=[]),
    })
    load_functions(SERVER / "chat/background/task.py", (
        "_execute_background_chat", "run_background_chat", "_update_session_status", "_json_safe_result",
    ), harness.code)
    update_status = MagicMock(wraps=harness.code["_update_session_status"])
    harness.code["_update_session_status"] = update_status

    result = harness.code["run_background_chat"](
        SimpleNamespace(request=SimpleNamespace(id=TASK_ID)),
        user_id=USER_ID,
        session_id=SESSION_ID,
        initial_message="检查服务故障",
    )
    sent = [json.loads(call.args[0]) for call in websocket.send.call_args_list]
    error_reported = any(message.get("type") == "error" for message in sent)
    assert observed["first_chunks"] == 1, (result, observed)
    assert observed["workflow_limit"] == 1800
    if fault == "read_timeout":
        assert observed["network_error"] == "ReadTimeout"
    elif fault == "disconnect":
        assert observed["network_error"] == "RemoteProtocolError"
    assert error_reported, "The workflow must report the transport error or timeout"
    assert observed["heartbeat_stopped"] is True
    success_updates = sum(call.args[1] == "completed" for call in update_status.call_args_list)
    label = {
        "read_timeout": "先输出，再触发网络读取超时",
        "disconnect": "先输出，随后连接中途断开",
        "silent_until_workflow_timeout": "先输出，随后一直静默，触发任务总超时",
    }[fault]
    print("断联中文结果：" + json.dumps({
        "场景": label,
        "已收到模型首段输出": "是",
        "流式处理报告了错误": "是" if error_reported else "否",
        "心跳已停止": "是" if observed["heartbeat_stopped"] else "否",
        "后台任务返回状态": result["status"],
        "数据库会话状态": harness.db.session_status,
        "标记成功次数": success_updates,
        "错误应该标记失败": "是",
        "真实模型接口": "本地 HTTP 故障模拟器",
    }, ensure_ascii=False))
    assert result["status"] == "failed", "A network failure must not become a completed investigation"
    assert harness.db.session_status == "failed"
    update_status.assert_called_once_with(SESSION_ID, "failed", user_id=USER_ID)
    assert success_updates == 0
    harness.code["_ensure_llm_context_history"].assert_not_called()
    harness.code["_extract_tool_calls_for_viz"].assert_not_called()


@pytest.mark.parametrize("background", [True, False], ids=["background", "foreground"])
def test_error_propagation_preserves_reporting_and_cost_tracking(harness, background):
    """Stream errors retain reporting and cost tracking, with background errors re-raised."""
    original_error = RuntimeError("model stream disconnected")

    class InterruptedWorkflow:
        async def stream(self, state):
            """Yield one token before raising the original stream error."""
            yield "token", "查"
            raise original_error

    async def scenario():
        """Check error handling and heartbeat cleanup in the chosen execution mode."""
        state = SimpleNamespace(session_id=SESSION_ID, is_background=background)
        websocket = SimpleNamespace(send=AsyncMock())
        if background:
            with pytest.raises(RuntimeError) as caught:
                await harness.code["process_workflow_async"](
                    InterruptedWorkflow(), state, websocket, USER_ID,
                )
            assert caught.value is original_error
        else:
            await harness.code["process_workflow_async"](
                InterruptedWorkflow(), state, websocket, USER_ID,
            )
        # Let the existing scheduled cost update run before closing the test loop.
        await asyncio.sleep(0)
        harness.code["update_api_cost_cache_async"].assert_awaited_once_with(USER_ID)
        sent = [json.loads(call.args[0]) for call in websocket.send.call_args_list]
        assert any(message.get("type") == "error" for message in sent)
        assert not any(task.get_name() == "background-session-heartbeat" for task in asyncio.all_tasks())

    asyncio.run(scenario())

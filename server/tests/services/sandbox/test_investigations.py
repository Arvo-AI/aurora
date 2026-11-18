"""Real agent graph with a deterministic test double, not an accuracy benchmark."""

import asyncio
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import ToolException

from services.sandbox.evidence import CASE_IDS, load_case
from services.sandbox.models import Diagnosis
from services.sandbox.runner import InvestigationContext, SandboxUnavailable, investigate


def diagnosis(prefix):
    return {
        "root_cause": {"explanation": "A test finding supported by this incident's records.", "evidence_ids": [f"{prefix}-02", f"{prefix}-05"]},
        "observations": [
            {"explanation": "The application measurements changed in this window.", "evidence_ids": [f"{prefix}-02"]},
            {"explanation": "The deployment records provide additional context.", "evidence_ids": [f"{prefix}-05"]},
        ],
        "alternative": {"explanation": "A competing explanation needs additional evidence.", "evidence_ids": [f"{prefix}-02"]},
        "uncertainty": "These test-double findings do not establish diagnostic accuracy.",
        "next_check": "Inspect the next relevant measurement before acting.",
    }


class ToolUsingTestModel(BaseChatModel):
    fail_prefix: str = ""
    stop_without_submission: bool = False

    @property
    def _llm_type(self):
        return "sandbox-test-double"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if self.stop_without_submission:
            message = AIMessage(content="An unstructured answer is not a submitted diagnosis.")
        elif not tool_messages:
            message = AIMessage(content="", tool_calls=[{"name": "list_evidence", "args": {}, "id": "list"}])
        else:
            records = json.loads(tool_messages[0].content)
            prefix = records[0]["id"].split("-")[0]
            if prefix == self.fail_prefix:
                raise RuntimeError("Deliberate independent case failure")
            if len(tool_messages) == 1:
                message = AIMessage(content="", tool_calls=[
                    {"name": "read_evidence", "args": {"evidence_id": f"{prefix}-02"}, "id": "read-metrics"},
                    {"name": "read_evidence", "args": {"evidence_id": f"{prefix}-05"}, "id": "read-change"},
                ])
            else:
                message = AIMessage(content="", tool_calls=[{"name": "submit_diagnosis", "args": diagnosis(prefix), "id": "submit"}])
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_three_concurrent_graphs_have_distinct_evidence_and_run_ids():
    async def run():
        return await asyncio.gather(*[
            investigate(case_id, model=ToolUsingTestModel()) for case_id in CASE_IDS
        ])

    results = asyncio.run(run())
    assert len({result["run_id"] for result in results}) == 3
    for case_id, result in zip(CASE_IDS, results):
        prefix = case_id[-1]
        assert result["case_id"] == case_id
        assert result["retrieved_evidence_ids"] == [f"{prefix}-02", f"{prefix}-05"]
        assert Diagnosis.model_validate(result["diagnosis"]).cited_ids() <= set(result["retrieved_evidence_ids"])
        assert [call["tool"] for call in result["tool_calls"]] == ["list_evidence", "read_evidence", "read_evidence", "submit_diagnosis"]


def test_one_failed_graph_does_not_cancel_other_cases():
    async def run():
        return await asyncio.gather(*[
            investigate(case_id, model=ToolUsingTestModel(fail_prefix="b")) for case_id in CASE_IDS
        ], return_exceptions=True)

    results = asyncio.run(run())
    assert results[0]["case_id"] == "case-a"
    assert isinstance(results[1], RuntimeError)
    assert results[2]["case_id"] == "case-c"


def test_case_cannot_read_another_cases_records():
    context = InvestigationContext("case-a")
    with pytest.raises(ToolException):
        context.read_evidence("b-02")
    assert context.retrieved == set()


def test_cannot_submit_unread_or_invented_citations():
    context = InvestigationContext("case-a")
    with pytest.raises(ToolException):
        context.submit_diagnosis(**diagnosis("a"))
    context.read_evidence("a-02")
    context.read_evidence("a-05")
    invalid = diagnosis("a")
    invalid["root_cause"]["evidence_ids"].append("a-imaginary")
    with pytest.raises(ToolException):
        context.submit_diagnosis(**invalid)
    assert context.diagnosis is None


def test_repeated_runs_start_with_fresh_context():
    first = InvestigationContext("case-a")
    first.read_evidence("a-02")
    first.case.evidence[0].content = "mutated by a test"
    second = InvestigationContext("case-a")
    assert not second.retrieved
    assert not second.tool_calls
    assert second.case.evidence[0].content != "mutated by a test"


def test_no_structured_submission_is_a_failure_not_a_fake_success():
    with pytest.raises(SandboxUnavailable, match="No result was substituted"):
        asyncio.run(investigate("case-a", model=ToolUsingTestModel(stop_without_submission=True)))


@pytest.mark.parametrize("case_id", ["../expected_cases", "case-d", "", "CASE-A"])
def test_only_allowlisted_bundles_can_be_loaded(case_id):
    with pytest.raises(ValueError):
        load_case(case_id)


def test_sample_tools_do_not_include_customer_or_execution_capabilities():
    assert {tool.name for tool in InvestigationContext("case-a").tools()} == {
        "list_evidence", "read_evidence", "submit_diagnosis",
    }


def test_missing_model_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv("MAIN_MODEL", raising=False)
    with pytest.raises(SandboxUnavailable, match="Set MAIN_MODEL"):
        asyncio.run(investigate("case-a"))


def test_three_http_requests_use_independent_real_graphs(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from flask import Flask
    from routes import sandbox_routes

    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")

    async def run_with_test_model(case_id):
        return await investigate(case_id, model=ToolUsingTestModel(fail_prefix="b"))

    monkeypatch.setattr(sandbox_routes, "investigate", run_with_test_model)
    app = Flask(__name__)
    app.register_blueprint(sandbox_routes.sandbox_bp)

    def post_case(case_id):
        with app.test_client() as client:
            response = client.post(
                "/sandbox/investigate", json={"case_id": case_id},
                headers={"X-Internal-Secret": "test-internal-secret"},
            )
            return response.status_code, response.json

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(post_case, CASE_IDS))
    assert [status for status, _ in results] == [200, 502, 200]
    assert results[0][1]["retrieved_evidence_ids"] == ["a-02", "a-05"]
    assert results[2][1]["retrieved_evidence_ids"] == ["c-02", "c-05"]


def test_provider_quota_error_is_actionable_without_private_details(monkeypatch):
    class QuotaModel(ToolUsingTestModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            error = RuntimeError("billing quota exhausted; private-provider-payload")
            error.status_code = 429
            raise error

    with pytest.raises(SandboxUnavailable) as caught:
        asyncio.run(investigate("case-a", model=QuotaModel()))
    assert "quota or billing limit" in str(caught.value)
    assert "private-provider-payload" not in str(caught.value)


def test_http_clients_are_not_reused_between_request_event_loops(monkeypatch):
    from services.sandbox import runner

    clients = []

    def model_factory(model_name, **kwargs):
        client = kwargs["http_async_client"]
        assert not client.is_closed
        clients.append(client)
        return ToolUsingTestModel()

    monkeypatch.setenv("MAIN_MODEL", "openai/test-model")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openai")
    monkeypatch.setattr(runner, "create_sandbox_model", model_factory)
    # Mirrors two Flask requests, each running a coroutine on a separate loop.
    first = asyncio.run(investigate("case-a"))
    second = asyncio.run(investigate("case-b"))
    assert first["case_id"] == "case-a" and second["case_id"] == "case-b"
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert all(client.is_closed for client in clients)


def test_http_client_is_closed_when_a_graph_fails(monkeypatch):
    from services.sandbox import runner

    clients = []

    def model_factory(model_name, **kwargs):
        clients.append(kwargs["http_async_client"])
        return ToolUsingTestModel(fail_prefix="b")

    monkeypatch.setenv("MAIN_MODEL", "openai/test-model")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openai")
    monkeypatch.setattr(runner, "create_sandbox_model", model_factory)
    with pytest.raises(RuntimeError, match="Deliberate"):
        asyncio.run(investigate("case-b"))
    assert clients[0].is_closed


def test_execution_deadline_cancels_model_work_and_closes_client(monkeypatch):
    from services.sandbox import runner

    clients = []
    cancelled = []

    class SlowModel(ToolUsingTestModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

    def model_factory(model_name, **kwargs):
        clients.append(kwargs["http_async_client"])
        return SlowModel()

    monkeypatch.setenv("MAIN_MODEL", "openai/test-model")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openai")
    monkeypatch.setattr(runner, "create_sandbox_model", model_factory)
    with pytest.raises(TimeoutError):
        asyncio.run(investigate("case-a", timeout_seconds=0.1))
    assert cancelled == [True]
    assert clients[0].is_closed


@pytest.mark.parametrize("status", [401, 403, 429])
def test_provider_access_and_throttle_errors_hide_private_payloads(status):
    class RejectedModel(ToolUsingTestModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            error = RuntimeError("private-provider-payload")
            error.status_code = status
            raise error

    with pytest.raises(SandboxUnavailable) as caught:
        asyncio.run(investigate("case-a", model=RejectedModel()))
    assert "private-provider-payload" not in str(caught.value)
    assert "limiting requests" in str(caught.value) if status == 429 else "credentials or model access" in str(caught.value)


def test_invalid_tool_arguments_are_reported_without_reading_other_evidence():
    context = InvestigationContext("case-a")
    read_tool = next(tool for tool in context.tools() if tool.name == "read_evidence")
    assert isinstance(read_tool.invoke({"evidence_id": []}), str)
    assert context.retrieved == set()
    assert context.diagnosis is None


def test_unending_tool_calls_stop_at_graph_recursion_budget():
    from langgraph.errors import GraphRecursionError

    class LoopingModel(ToolUsingTestModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="", tool_calls=[{"name": "list_evidence", "args": {}, "id": "repeat"}],
            ))])

    with pytest.raises(GraphRecursionError):
        asyncio.run(investigate("case-a", model=LoopingModel()))

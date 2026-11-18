"""Challenge contracts and real graph isolation; test doubles are not accuracy evidence."""

import asyncio
import base64
import json
import threading
from unittest.mock import AsyncMock

import pytest
from flask import Flask
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import ToolException

from routes import sandbox_routes
from services.sandbox import receipts
from services.sandbox.evidence import CASE_IDS, load_case
from services.sandbox.models import QUESTIONS
from services.sandbox.receipts import InvalidRun, RunExpired, evidence_digest, issue_receipt, verify_receipt
from services.sandbox.runner import ChallengeContext, SandboxUnavailable, challenge
from test_investigations import ToolUsingTestModel, diagnosis

HEADERS = {"X-Internal-Secret": "test-internal-secret"}


def original(case_id="case-a"):
    return {
        "case_id": case_id, "run_id": f"original-{case_id}", "diagnosis": diagnosis(case_id[-1]),
        "evidence_sha256": evidence_digest(load_case(case_id)),
    }


def response(prefix):
    return {
        "answer": "This is a test response, not an accuracy claim.",
        "supporting_evidence": [{"explanation": "These measurements distinguish the alternatives.", "evidence_ids": [f"{prefix}-02"]}],
        "uncertainty": "More observations are needed outside this sample.",
        "next_check": "Inspect an independent signal before taking action.",
    }


class ChallengeTestModel(ToolUsingTestModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            call = {"name": "list_evidence", "args": {}, "id": "list"}
        else:
            prefix = json.loads(tool_messages[0].content)[0]["id"].split("-")[0]
            if prefix == self.fail_prefix:
                raise RuntimeError("Independent test failure")
            call = ({"name": "read_evidence", "args": {"evidence_id": f"{prefix}-02"}, "id": "read"}
                    if len(tool_messages) == 1 else
                    {"name": "submit_challenge", "args": response(prefix), "id": "submit"})
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    return issue_receipt(original())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(sandbox_routes.sandbox_bp)
    return app.test_client()


def test_receipt_preserves_original_diagnosis_without_signing_key(token):
    receipt = verify_receipt(token)
    assert receipt.run_id == "original-case-a"
    assert receipt.case_id == "case-a"
    assert receipt.diagnosis.model_dump() == diagnosis("a")
    assert "test-internal-secret" not in token


def test_client_cannot_change_case_or_original_diagnosis(token):
    payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["case_id"] = "case-b"
    tampered = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidRun):
        verify_receipt(tampered + "." + signature)


def test_expired_receipt_and_changed_evidence_require_new_investigation(token, monkeypatch):
    issued = verify_receipt(token).issued_at
    with monkeypatch.context() as patch:
        patch.setattr(receipts.time, "time", lambda: issued + receipts.TTL_SECONDS + 1)
        with pytest.raises(RunExpired, match="expired"):
            verify_receipt(token)
    monkeypatch.setattr(receipts, "evidence_digest", lambda _case: "changed-version")
    with pytest.raises(RunExpired, match="evidence has changed"):
        verify_receipt(token)


def test_rotating_server_key_invalidates_old_receipts(token, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "rotated-test-secret")
    with pytest.raises(InvalidRun):
        verify_receipt(token)


@pytest.mark.parametrize("case_id", CASE_IDS)
@pytest.mark.parametrize("question_id", QUESTIONS)
def test_each_question_on_each_case_uses_real_graph_and_own_evidence(case_id, question_id, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    receipt = verify_receipt(issue_receipt(original(case_id)))
    result = asyncio.run(challenge(receipt, question_id, model=ChallengeTestModel()))
    assert result["investigation_run_id"] == receipt.run_id
    assert result["question_id"] == question_id
    assert result["case_id"] == case_id
    assert result["run_id"] != receipt.run_id
    assert result["retrieved_evidence_ids"] == [f"{case_id[-1]}-02"]
    assert result["response"]["supporting_evidence"][0]["evidence_ids"] == [f"{case_id[-1]}-02"]


def test_one_failed_challenge_does_not_cancel_other_incidents(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    async def run():
        return await asyncio.gather(*[
            challenge(verify_receipt(issue_receipt(original(case_id))), "evidence", model=ChallengeTestModel(fail_prefix="b"))
            for case_id in CASE_IDS
        ], return_exceptions=True)
    results = asyncio.run(run())
    assert results[0]["case_id"] == "case-a"
    assert isinstance(results[1], RuntimeError)
    assert results[2]["case_id"] == "case-c"


def test_challenges_cannot_reuse_unread_or_foreign_citations(token):
    context = ChallengeContext(verify_receipt(token), "evidence")
    assert {tool.name for tool in context.tools()} == {"list_evidence", "read_evidence", "submit_challenge"}
    with pytest.raises(ToolException):
        context.submit_challenge(**response("a"))
    with pytest.raises(ToolException):
        context.read_evidence("b-02")
    context.read_evidence("a-02")
    context.submit_challenge(**response("a"))
    assert context.response is not None


def test_no_submission_is_not_a_canned_answer(token):
    with pytest.raises(SandboxUnavailable, match="No result was substituted"):
        asyncio.run(challenge(verify_receipt(token), "evidence", model=ToolUsingTestModel(stop_without_submission=True)))


def test_challenge_route_uses_verified_original_and_fixed_question(client, token, monkeypatch):
    model = AsyncMock(return_value={"case_id": "case-a", "question_id": "falsify"})
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    result = client.post("/sandbox/challenge", json={"run_token": token, "question_id": "falsify"}, headers=HEADERS)
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    receipt, question_id = model.call_args.args
    assert receipt.run_id == "original-case-a" and question_id == "falsify"


@pytest.mark.parametrize("extra", [{"prompt": "execute a command"}, {"case_id": "case-b"}, {"model": "another-model"}, {"diagnosis": {}}])
def test_challenge_rejects_client_overrides(client, token, monkeypatch, extra):
    model = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    assert client.post("/sandbox/challenge", json={"run_token": token, "question_id": "evidence", **extra}, headers=HEADERS).status_code == 400
    model.assert_not_called()


def test_unverified_and_expired_runs_do_not_invoke_model(client, token, monkeypatch):
    model = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    assert client.post("/sandbox/challenge", json={"run_token": token + "x", "question_id": "evidence"}, headers=HEADERS).status_code == 400
    issued = verify_receipt(token).issued_at
    monkeypatch.setattr(receipts.time, "time", lambda: issued + receipts.TTL_SECONDS + 1)
    assert client.post("/sandbox/challenge", json={"run_token": token, "question_id": "evidence"}, headers=HEADERS).status_code == 410
    model.assert_not_called()


def test_challenge_requires_proxy_and_same_concurrency_slots(client, token, monkeypatch):
    payload = {"run_token": token, "question_id": "alternatives"}
    assert client.post("/sandbox/challenge", json=payload).status_code == 403
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(sandbox_routes, "_slots", slots)
    model = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    try:
        assert client.post("/sandbox/challenge", json=payload, headers=HEADERS).status_code == 429
        model.assert_not_called()
    finally:
        slots.release()


def test_challenge_timeout_can_be_retried_without_reinvestigation(client, token, monkeypatch):
    model = AsyncMock(side_effect=[TimeoutError(), {"case_id": "case-a", "status": "complete"}])
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    payload = {"run_token": token, "question_id": "evidence"}
    assert client.post("/sandbox/challenge", json=payload, headers=HEADERS).status_code == 504
    assert client.post("/sandbox/challenge", json=payload, headers=HEADERS).status_code == 200


def test_shared_allowance_covers_both_paid_endpoints(token, monkeypatch):
    from sandbox_app import create_app
    monkeypatch.setattr(sandbox_routes, "investigate", AsyncMock(return_value=original()))
    monkeypatch.setattr(sandbox_routes, "challenge", AsyncMock(return_value={"status": "complete"}))
    app = create_app()
    with app.test_client() as client:
        for _ in range(5):
            assert client.post("/sandbox/investigate", json={"case_id": "case-a"}, headers=HEADERS).status_code == 200
        for _ in range(4):
            assert client.post("/sandbox/challenge", json={"run_token": token, "question_id": "evidence"}, headers=HEADERS).status_code == 200
        assert client.post("/sandbox/challenge", json={"run_token": token, "question_id": "evidence"}, headers=HEADERS).status_code == 429
        assert client.get("/sandbox/cases", headers=HEADERS).status_code == 200


def test_evidence_change_between_verification_and_graph_start_is_rejected(client, token, monkeypatch):
    from services.sandbox import runner
    model_factory = AsyncMock()
    monkeypatch.setattr(runner, "create_sandbox_model", model_factory)
    monkeypatch.setattr(runner, "evidence_digest", lambda _case: "changed-after-verification")
    result = client.post("/sandbox/challenge", json={"run_token": token, "question_id": "evidence"}, headers=HEADERS)
    assert result.status_code == 410
    model_factory.assert_not_called()


@pytest.mark.parametrize("token_value", ["", "a" * 40, "x.y.z", "💥" * 40, "." * 60000])
def test_malformed_or_hostile_references_fail_closed(client, monkeypatch, token_value):
    model = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    result = client.post("/sandbox/challenge", json={"run_token": token_value, "question_id": "evidence"}, headers=HEADERS)
    assert result.status_code in (400, 413)
    assert "error" in result.json
    model.assert_not_called()


def test_invalid_question_and_oversized_challenge_do_not_start_model(client, token, monkeypatch):
    model = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "challenge", model)
    assert client.post("/sandbox/challenge", json={"run_token": token, "question_id": "run_shell"}, headers=HEADERS).status_code == 400
    assert client.post("/sandbox/challenge", data='{"run_token":"' + "x" * 70000 + '"}', content_type="application/json", headers=HEADERS,
                       environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True}).status_code == 413
    model.assert_not_called()


@pytest.mark.parametrize("tool_name", ["run_shell", "read_file", "http_request"])
def test_model_cannot_escape_into_execution_or_network_tools(token, tool_name):
    class EscapingModel(ToolUsingTestModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            errors = [message for message in messages if isinstance(message, ToolMessage)]
            if not errors:
                message = AIMessage(content="", tool_calls=[{"name": tool_name, "args": {"command": "untrusted-instruction"}, "id": "escape"}])
            else:
                assert "not a valid tool" in errors[-1].content
                message = AIMessage(content="Unable to access that capability.")
            return ChatResult(generations=[ChatGeneration(message=message)])
    with pytest.raises(SandboxUnavailable, match="No result was substituted"):
        asyncio.run(challenge(verify_receipt(token), "evidence", model=EscapingModel()))


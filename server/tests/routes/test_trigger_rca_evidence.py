"""POST /api/incidents/trigger-rca with pasted evidence, and how the RCA prompt renders it."""

import json

import pytest
from flask import Flask

from chat.background.rca_prompt_builder import PASTED_EVIDENCE_MAX, build_rca_prompt

LOG_LINE = 'ERROR QueuePool limit of size 5 overflow 0 reached, "connection timed out"'


@pytest.fixture
def post(monkeypatch):
    import routes.incidents_routes as mod

    calls = []

    def fake_trigger_rca(**kwargs):
        calls.append(kwargs)
        return json.dumps({"status": "ok", "incident_id": "inc-1", "rca_session_id": "sess-1"})

    monkeypatch.setattr("chat.backend.agent.tools.trigger_rca_tool.trigger_rca", fake_trigger_rca)

    def _run(body):
        rbac_globals = mod.trigger_rca_from_chat.__globals__
        for name, value in {
            "get_user_id_from_request": lambda: "user-1",
            "get_org_id_from_request": lambda: "org-1",
            "_audit_auth_failure": lambda *a, **k: None,
            "enforce_with_reload": lambda u, o, res, act: True,
        }.items():
            monkeypatch.setitem(rbac_globals, name, value)
        app = Flask(__name__)
        with app.test_request_context("/api/incidents/trigger-rca", method="POST", json=body):
            rv = mod.trigger_rca_from_chat()
            resp, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return resp.get_json(), status

    return _run, calls


def test_evidence_is_passed_through_to_the_rca(post):
    run, calls = post
    body, status = run({"title": "checkout 5xx", "issue_description": "checkout 5xx", "evidence": f"  {LOG_LINE}\n"})
    assert status == 200
    assert body["incident_id"] == "inc-1"
    assert calls[0]["evidence"] == LOG_LINE
    assert calls[0]["user_id"] == "user-1"


def test_evidence_alone_is_enough(post):
    run, calls = post
    _, status = run({"evidence": LOG_LINE})
    assert status == 200
    assert calls[0]["issue_description"] == "Investigate the pasted evidence."


def test_evidence_longer_than_the_description_cap_is_accepted(post):
    run, calls = post
    _, status = run({"issue_description": "x", "evidence": "a" * PASTED_EVIDENCE_MAX})
    assert status == 200
    assert len(calls[0]["evidence"]) == PASTED_EVIDENCE_MAX


@pytest.mark.parametrize("evidence", ["a" * (PASTED_EVIDENCE_MAX + 1), ["not", "text"], 42])
def test_oversized_or_non_text_evidence_is_400(post, evidence):
    run, calls = post
    body, status = run({"issue_description": "x", "evidence": evidence})
    assert status == 400
    assert "evidence" in body["error"]
    assert calls == []


def test_description_is_still_required_without_evidence(post):
    run, calls = post
    _, status = run({"title": "only a title"})
    assert status == 400
    assert calls == []


def test_prompt_renders_evidence_verbatim_outside_the_json_payload():
    payload = {"title": "t", "description": "d", "evidence": LOG_LINE}
    prompt, rail_text = build_rca_prompt("chat", "t", payload)
    payload_block = prompt.split("<alert_payload>")[1].split("</alert_payload>")[0]
    evidence_block = prompt.split("<pasted_evidence>")[1].split("</pasted_evidence>")[0]
    assert LOG_LINE in evidence_block  # unescaped, so the agent can quote it
    assert "evidence" not in json.loads(payload_block)
    assert LOG_LINE in rail_text  # still screened by the input rail
    assert payload["evidence"] == LOG_LINE  # caller's dict is not mutated


def test_prompt_is_unchanged_without_evidence_and_for_webhook_sources():
    prompt, _ = build_rca_prompt("chat", "t", {"title": "t", "description": "d"})
    assert "PASTED EVIDENCE" not in prompt
    prompt, _ = build_rca_prompt("grafana", "t", {"title": "t", "evidence": LOG_LINE})
    assert "PASTED EVIDENCE" not in prompt
    assert "QueuePool limit of size 5" in prompt

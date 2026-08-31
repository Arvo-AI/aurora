"""run_recurrence_check: mode gating, clamping, timeout — agent fully stubbed."""

import asyncio
import os
import sys
import types
import uuid
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

import services.correlation.recurrence_agent as ra  # noqa: E402
from services.correlation.recurrence_fold import RecurrenceVerdict  # noqa: E402

INCIDENT = str(uuid.uuid4())
ANCHOR = str(uuid.uuid4())

# Captured at import time, before any fixture patches the module attribute.
_ORIGINAL_CLAMP = ra._clamp_claimed_id


def _ctx():
    return {
        "org_id": "org-1",
        "title": "High CPU",
        "service": "api",
        "source_type": "datadog",
        "severity": "critical",
        "started_at_iso": "2026-08-27T12:00:00",
        "fired_at_iso": "2026-08-27T12:00:00",
        "summary": "root cause: bad deploy",
        "hint": {"incident_id": ANCHOR, "score": 0.7, "strategy": "similarity"},
    }


@pytest.fixture
def harness(monkeypatch):
    """Stub every collaborator; return the mocks for assertions."""
    fake_llm = types.ModuleType("chat.backend.agent.llm")
    fake_llm.ModelConfig = MagicMock(RECURRENCE_AGENT_MODEL="test-model")
    monkeypatch.setitem(sys.modules, "chat.backend.agent.llm", fake_llm)

    mocks = types.SimpleNamespace(
        get_existing_verdict=MagicMock(return_value=None),
        fetch_ctx=MagicMock(return_value=_ctx()),
        persist_verdict=MagicMock(return_value=True),
        fold_incident=MagicMock(),
        clamp=MagicMock(return_value=(ANCHOR, None)),
    )
    monkeypatch.setattr(ra, "get_existing_verdict", mocks.get_existing_verdict)
    monkeypatch.setattr(ra, "_fetch_incident_context", mocks.fetch_ctx)
    monkeypatch.setattr(ra, "persist_verdict", mocks.persist_verdict)
    monkeypatch.setattr(ra, "fold_incident", mocks.fold_incident)
    monkeypatch.setattr(ra, "_clamp_claimed_id", mocks.clamp)
    return mocks


def _stub_agent(monkeypatch, verdict=None, delay=0.0):
    async def _fake_run_agent(**kwargs):
        if delay:
            await asyncio.sleep(delay)
        if verdict is not None:
            kwargs["captured"]["verdict"] = verdict

    monkeypatch.setattr(ra, "_run_agent", _fake_run_agent)


def _run(monkeypatch, mode):
    monkeypatch.setenv("RECURRENCE_DETECTION_MODE", mode)
    ra.run_recurrence_check(
        incident_id=INCIDENT, user_id="u1", session_id="s1", decision_point="after"
    )


class TestModeGating:
    def test_off_does_nothing(self, harness, monkeypatch):
        _stub_agent(monkeypatch, verdict=RecurrenceVerdict(recurrence_of=ANCHOR, reasoning="r"))
        _run(monkeypatch, "off")
        harness.get_existing_verdict.assert_not_called()
        harness.fetch_ctx.assert_not_called()
        harness.persist_verdict.assert_not_called()
        harness.fold_incident.assert_not_called()

    def test_shadow_persists_verdict_without_folding(self, harness, monkeypatch):
        _stub_agent(monkeypatch, verdict=RecurrenceVerdict(recurrence_of=ANCHOR, reasoning="same cause"))
        _run(monkeypatch, "shadow")
        harness.fold_incident.assert_not_called()
        harness.persist_verdict.assert_called_once()
        kwargs = harness.persist_verdict.call_args.kwargs
        assert kwargs["mode"] == "shadow"
        assert kwargs["folded"] is False
        assert kwargs["accepted_recurrence_of"] == ANCHOR
        assert kwargs["claimed_recurrence_of"] == ANCHOR
        assert kwargs["correlator_score"] == pytest.approx(0.7)
        assert kwargs["reject_reason"] is None

    def test_live_folds_accepted_claim(self, harness, monkeypatch):
        _stub_agent(monkeypatch, verdict=RecurrenceVerdict(recurrence_of=ANCHOR, reasoning="same cause"))
        _run(monkeypatch, "live")
        harness.fold_incident.assert_called_once()
        kwargs = harness.fold_incident.call_args.kwargs
        assert kwargs["incident_id"] == INCIDENT
        assert kwargs["claimed_recurrence_of"] == ANCHOR
        assert kwargs["mode"] == "live"
        # fold_incident writes the verdict row itself on this path
        harness.persist_verdict.assert_not_called()

    def test_live_new_verdict_persists_without_folding(self, harness, monkeypatch):
        _stub_agent(monkeypatch, verdict=RecurrenceVerdict(recurrence_of=None, reasoning="new failure"))
        _run(monkeypatch, "live")
        harness.fold_incident.assert_not_called()
        kwargs = harness.persist_verdict.call_args.kwargs
        assert kwargs["accepted_recurrence_of"] is None
        assert kwargs["folded"] is False


class TestFailurePaths:
    def test_no_verdict_defaults_to_new(self, harness, monkeypatch):
        _stub_agent(monkeypatch, verdict=None)
        _run(monkeypatch, "shadow")
        harness.fold_incident.assert_not_called()
        kwargs = harness.persist_verdict.call_args.kwargs
        assert kwargs["reject_reason"] == "no_verdict"
        assert kwargs["accepted_recurrence_of"] is None

    def test_timeout_defaults_to_new(self, harness, monkeypatch):
        monkeypatch.setattr(ra, "get_agent_timeout_seconds", lambda: 0.05)
        _stub_agent(
            monkeypatch,
            verdict=RecurrenceVerdict(recurrence_of=ANCHOR, reasoning="r"),
            delay=5.0,
        )
        _run(monkeypatch, "live")
        harness.fold_incident.assert_not_called()
        kwargs = harness.persist_verdict.call_args.kwargs
        assert kwargs["reject_reason"] == "timeout"
        assert kwargs["accepted_recurrence_of"] is None

    def test_invented_id_clamps_to_invalid_id(self, harness, monkeypatch):
        # Real clamp: a malformed id short-circuits before ever reaching the DB.
        monkeypatch.setattr(ra, "_clamp_claimed_id", _ORIGINAL_CLAMP)
        _stub_agent(
            monkeypatch,
            verdict=RecurrenceVerdict(recurrence_of="not-a-real-id", reasoning="r"),
        )
        _run(monkeypatch, "live")
        harness.fold_incident.assert_not_called()
        kwargs = harness.persist_verdict.call_args.kwargs
        assert kwargs["reject_reason"] == "invalid_id"
        assert kwargs["accepted_recurrence_of"] is None
        assert kwargs["claimed_recurrence_of"] == "not-a-real-id"

    def test_self_reference_claim_clamped(self, harness, monkeypatch):
        monkeypatch.setattr(ra, "_clamp_claimed_id", _ORIGINAL_CLAMP)
        _stub_agent(
            monkeypatch,
            verdict=RecurrenceVerdict(recurrence_of=INCIDENT, reasoning="r"),
        )
        _run(monkeypatch, "live")
        harness.fold_incident.assert_not_called()
        kwargs = harness.persist_verdict.call_args.kwargs
        assert kwargs["reject_reason"] == "self_reference"
        assert kwargs["accepted_recurrence_of"] is None


class TestIdempotency:
    def test_existing_verdict_skips_rerun(self, harness, monkeypatch):
        harness.get_existing_verdict.return_value = {"folded": False, "mode": "shadow"}
        _stub_agent(monkeypatch, verdict=RecurrenceVerdict(recurrence_of=ANCHOR, reasoning="r"))
        _run(monkeypatch, "shadow")
        harness.fetch_ctx.assert_not_called()
        harness.persist_verdict.assert_not_called()
        harness.fold_incident.assert_not_called()

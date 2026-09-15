"""The four recommender LLM stages must parse Gemini thinking-mode list content.

Review of PR #612 flagged four ``str(response.content)`` call sites in
``chat/background/recommender.py``. On Gemini thinking models (google/vertex
providers, ``include_thoughts`` on) ``response.content`` is a list of blocks, so
``str()`` of it is a Python repr: ``json.loads`` rejects it and the enrichment /
summary regexes never match. Two stages logged a warning and returned nothing;
the other two failed silently. The fixture holds the real gemini-3.6-flash
responses each stage received on 2026-09-08 (see tests/fixtures/).
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from chat.background import recommender as R
from chat.background.citation_extractor import Citation
from chat.background.suggestion_extractor import Suggestion

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gemini_thinking_responses.json"
_RESPONSES = {r["stage"]: r["content"] for r in json.loads(_FIXTURE.read_text())["responses"]}

_CREATE = "chat.backend.agent.providers.create_chat_model"

# _extract_hypotheses skips the LLM call for reasoning under 100 characters.
_REASONING = """Alert: checkout-api p99 latency > 2s. Pod status [1]: 2 of 3 replicas in CrashLoopBackOff.
Pod logs [2] show OOMKilled events in the last 30 minutes. Deployment spec [3] has memory limit 256Mi,
while commit abc123 [4] bumped the in-memory cache from 100MB to 400MB. RDS metrics [5] are normal, so a
database slowdown is ruled out. The ALB target group was not inspected. Root cause confirmed: the memory
limit is too low for the new cache size; mitigation is a rollback of abc123 or raising the limit to 768Mi."""
_TRACKED = "chat.backend.agent.utils.llm_usage_tracker.tracked_invoke"


def _llm_returning(content):
    llm = MagicMock(name="llm")
    llm.invoke.return_value = SimpleNamespace(content=content)
    return llm


def _text_block(stage: str) -> str:
    return _RESPONSES[stage][1]["text"]


def _suggestions() -> list[Suggestion]:
    return [
        Suggestion(
            title="Raise checkout-api memory limit to 768Mi",
            description="validation: OOMKilled x14 in 30m; limit 256Mi",
            type="remediate", risk="medium",
            command="kubectl set resources deploy/checkout-api -n checkout --limits=memory=768Mi",
            rationale="pods OOMKilled after cache bump in abc123",
        ),
        Suggestion(
            title="Roll back commit abc123",
            description="validation: cache size 100MB->400MB in abc123",
            type="mitigation", risk="low",
            command="git revert abc123",
            rationale="commit introduced the memory growth",
        ),
    ]


def test_hypothesis_extraction_reads_the_text_block():
    expected = json.loads(R._strip_code_fences(_text_block("hypothesis_extraction")))

    with patch(_CREATE, return_value=_llm_returning(_RESPONSES["hypothesis_extraction"])):
        state = R._extract_hypotheses(_REASONING)

    assert len(state.hypotheses) == len(expected["hypotheses"]) >= 1
    assert len(state.ruled_out) == len(expected["ruled_out"]) >= 1
    assert state.unexplored == expected["unexplored"]
    assert any(h.status == "confirmed" for h in state.hypotheses)


def test_self_exec_planning_reads_the_text_block():
    planned = {c["command"] for c in json.loads(_text_block("self_exec_planning"))}
    citations = [Citation(1, "kubectl", "kubectl get pods -n checkout", "CrashLoopBackOff", datetime.now(), "c1")]
    executed: list[str] = []

    def fake_exec(spec, user_id, session_id):
        executed.append(spec["command"])
        return {"command": spec["command"], "provider": spec.get("provider", "general"),
                "output": "<stubbed>", "success": True, "rationale": spec.get("rationale", "")}

    with patch(_CREATE, return_value=_llm_returning(_RESPONSES["self_exec_planning"])), \
         patch.object(R, "_execute_single_diagnostic", side_effect=fake_exec):
        results = R._self_execute_safe_diagnostics(citations=citations, user_id="user-1", session_id="sess-1")

    assert 1 <= len(results) <= R._MAX_SELF_EXEC_CALLS
    assert executed and set(executed) <= planned


def test_fix_enrichment_applies_descriptions_and_summaries():
    suggestions = _suggestions()

    with patch(_CREATE, return_value=MagicMock(name="llm")), \
         patch(_TRACKED, return_value=SimpleNamespace(content=_RESPONSES["fix_enrichment"])), \
         patch.object(R, "_generate_summaries") as fallback:
        R._enrich_validated_fixes(suggestions, "reasoning", "user-1", "sess-1")

    fallback.assert_not_called()
    assert suggestions[0].description.startswith("Pod logs [2] show OOMKilled")
    assert suggestions[1].summary == "Restores the 100MB cache size so pods fit within the existing 256Mi limit."
    assert all(s.summary for s in suggestions)
    assert not any(s.description.startswith("validation:") for s in suggestions)


def test_summary_generation_sets_one_line_summaries():
    suggestions = _suggestions()

    with patch(_CREATE, return_value=MagicMock(name="llm")), \
         patch(_TRACKED, return_value=SimpleNamespace(content=_RESPONSES["summary_generation"])):
        R._generate_summaries(suggestions, "user-1", "sess-1")

    assert suggestions[0].summary == "Cache bump in abc123 exceeds 256Mi limit, causing 14 pod OOMKills in 30m."
    assert suggestions[1].summary == "Expanding cache size to 400MB introduced severe memory growth across service instances."

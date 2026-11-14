"""Tests for agent interpretation of free-form Startup Check facts.

The LLM is faked at the module boundary (``create_chat_model`` is imported
inside ``parse_freeform_facts``), so these cover the contract: accepted facts
must survive context validation, refused facts must say why, and every
failure mode must fail closed — the scan never loses the form data.
"""

import services.startup_check.context_llm as context_llm
from services.startup_check.context_llm import (
    MAX_FACTS,
    ParsedFacts,
    _LLMDecision,
    _LLMNode,
    parse_freeform_facts,
)


class _FakeInterpreter:
    """Stands in for model.with_structured_output(...).invoke()."""

    def __init__(self, result):
        self._result = result

    def invoke(self, prompt):
        return self._result


class _FakeModel:
    def __init__(self, result):
        self._result = result

    def with_structured_output(self, schema, include_raw=False, method=None):
        return _FakeInterpreter(self._result)


def _patch_llm(monkeypatch, parsed, chain=("test/premium",)):
    """Patch the model chain and provider factory used by the interpreter.

    ``chain`` is the ordered list of models; the patched factory returns the
    same ``result`` for every model in it.
    """
    result = {"parsed": parsed, "raw": None}
    monkeypatch.setattr(context_llm, "_model_chain", lambda: list(chain))
    monkeypatch.setattr(
        "chat.backend.agent.providers.create_chat_model",
        lambda *a, **kw: _FakeModel(result),
        raising=False,
    )


def _patch_chain_llm(monkeypatch, chain, behavior):
    """Patch the chain where each model behaves differently.

    ``behavior`` maps model name -> ParsedFacts (success), None (unparsable),
    or an Exception instance (API failure).
    """
    monkeypatch.setattr(context_llm, "_model_chain", lambda: list(chain))

    def factory(model_name, *a, **kw):
        outcome = behavior[model_name]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeModel({"parsed": outcome, "raw": None})

    monkeypatch.setattr(
        "chat.backend.agent.providers.create_chat_model", factory, raising=False
    )


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_mapped_fact_produces_context(monkeypatch):
    parsed = ParsedFacts(
        service_memory_mb={"api": 900},
        decisions=[_LLMDecision(index=1, action="mapped")],
    )
    _patch_llm(monkeypatch, parsed)

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "api memory", "value": "900 MB"}])

    assert ctx == {"service_memory_mb": {"api": 900.0}}
    assert refusals == []


def test_node_fact_maps_and_validates(monkeypatch):
    parsed = ParsedFacts(
        nodes=[_LLMNode(name="pool-a", cpu_cores=8, memory_mb=32768)],
        decisions=[_LLMDecision(index=1, action="mapped")],
    )
    _patch_llm(monkeypatch, parsed)

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "nodes", "value": "8 cores 32GB"}])

    assert ctx["nodes"][0]["cpu_cores"] == 8.0
    assert refusals == []


# ---------------------------------------------------------------------------
# Premium-first, free-fallback strategy
# ---------------------------------------------------------------------------


def test_premium_failure_falls_back_to_free(monkeypatch):
    """Opus quota exhausted (403) -> the free model still interprets."""
    good = ParsedFacts(
        service_memory_mb={"api": 900},
        decisions=[_LLMDecision(index=1, action="mapped")],
    )
    _patch_chain_llm(
        monkeypatch,
        chain=["test/opus", context_llm.FREE_FALLBACK_MODEL],
        behavior={
            "test/opus": RuntimeError("403 key limit exceeded"),
            context_llm.FREE_FALLBACK_MODEL: good,
        },
    )

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "api memory", "value": "900 MB"}])

    assert ctx == {"service_memory_mb": {"api": 900.0}}
    assert refusals == []


def test_premium_unparsable_falls_back_to_free(monkeypatch):
    """A premium model that returns garbage also triggers the fallback."""
    good = ParsedFacts(
        decisions=[_LLMDecision(index=1, action="refused", reason="gibberish")]
    )
    _patch_chain_llm(
        monkeypatch,
        chain=["test/opus", "test/free"],
        behavior={"test/opus": None, "test/free": good},
    )

    _, refusals = parse_freeform_facts("user-1", [{"name": "x", "value": "y"}])

    assert refusals == [{"name": "x", "reason": "gibberish"}]


def test_entire_chain_dead_fails_closed(monkeypatch):
    _patch_chain_llm(
        monkeypatch,
        chain=["test/opus", "test/free"],
        behavior={
            "test/opus": RuntimeError("403"),
            "test/free": RuntimeError("404 no endpoints"),
        },
    )

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "a", "value": "1"}])

    assert ctx == {}
    assert refusals == [{"name": "a", "reason": context_llm._FAIL_CLOSED_REASON}]


def test_explicit_env_model_disables_fallback(monkeypatch):
    """STARTUP_CHECK_FACT_MODEL set means exactly that model — one failure
    goes straight to fail-closed, no silent fallback to a different model."""
    monkeypatch.setenv("STARTUP_CHECK_FACT_MODEL", "test/only")
    assert context_llm._model_chain() == ["test/only"]
    monkeypatch.delenv("STARTUP_CHECK_FACT_MODEL")


def test_default_chain_is_premium_then_free(monkeypatch):
    monkeypatch.delenv("STARTUP_CHECK_FACT_MODEL", raising=False)
    monkeypatch.setattr(
        "chat.backend.agent.llm.ModelConfig.RCA_MODEL",
        "test/premium-rca",
        raising=False,
    )

    chain = context_llm._model_chain()

    assert chain[0] == "test/premium-rca"
    assert chain[-1] == context_llm.FREE_FALLBACK_MODEL





def test_gibberish_fact_refused_with_reason(monkeypatch):
    parsed = ParsedFacts(
        decisions=[_LLMDecision(index=1, action="refused", reason="gibberish")],
    )
    _patch_llm(monkeypatch, parsed)

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "zqx", "value": "blorp fnord"}])

    assert ctx == {}
    assert refusals == [{"name": "zqx", "reason": "gibberish"}]


def test_unmentioned_fact_is_refused_never_accepted(monkeypatch):
    """A fact the model mapped nothing for and never decided on must not
    slip through as accepted — fail closed per fact."""
    parsed = ParsedFacts(decisions=[])  # model ignored the fact entirely
    _patch_llm(monkeypatch, parsed)

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "region", "value": "us-east-1"}])

    assert ctx == {}
    assert refusals == [{"name": "region", "reason": "not interpreted by the agent"}]


def test_unknown_action_counts_as_refusal(monkeypatch):
    parsed = ParsedFacts(
        decisions=[_LLMDecision(index=1, action="maybe", reason="")],
    )
    _patch_llm(monkeypatch, parsed)

    _, refusals = parse_freeform_facts("user-1", [{"name": "x", "value": "y"}])

    assert refusals == [{"name": "x", "reason": "refused by the agent"}]


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_llm_exception_refuses_all(monkeypatch):
    import chat.backend.agent.providers as providers

    def boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(providers, "create_chat_model", boom)

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}])

    assert ctx == {}
    assert len(refusals) == 2
    assert all(r["reason"] == "the agent could not interpret custom facts right now" for r in refusals)


def test_unparsable_output_refuses_all(monkeypatch):
    _patch_llm(monkeypatch, None)  # parsed=None path

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "a", "value": "1"}])

    assert ctx == {}
    assert refusals == [{"name": "a", "reason": "the agent could not interpret custom facts right now"}]


def test_llm_output_failing_context_validation_refuses_all(monkeypatch):
    """If the model 'accepts' a value the shared gate rejects (negative MB),
    every fact is refused rather than a half-valid context going through."""
    parsed = ParsedFacts(
        service_memory_mb={"api": -5},  # invalid — must not survive
        decisions=[_LLMDecision(index=1, action="mapped")],
    )
    _patch_llm(monkeypatch, parsed)

    ctx, refusals = parse_freeform_facts("user-1", [{"name": "api memory", "value": "-5 MB"}])

    assert ctx == {}
    assert "failed validation" in refusals[0]["reason"]


# ---------------------------------------------------------------------------
# Input budgets
# ---------------------------------------------------------------------------


def test_over_limit_facts_refused_deterministically(monkeypatch):
    """Facts past the cap are refused without needing the model to look at
    them; the in-limit ones still go through the agent."""
    in_limit = [
        _LLMDecision(index=i, action="refused", reason="agent said no")
        for i in range(1, MAX_FACTS + 1)
    ]
    _patch_llm(monkeypatch, ParsedFacts(decisions=in_limit))

    facts = [{"name": f"fact-{i}", "value": "x"} for i in range(MAX_FACTS + 2)]
    ctx, refusals = parse_freeform_facts("user-1", facts)

    assert ctx == {}
    assert len(refusals) == MAX_FACTS + 2
    # Over-limit refusals are recorded during input sanitizing, so they
    # come first; the agent's refusals for the in-limit facts follow.
    assert "too many facts" in refusals[0]["reason"]
    assert refusals[MAX_FACTS - 2]["reason"] == "agent said no"


def test_empty_and_blank_facts_skipped(monkeypatch):
    ctx, refusals = parse_freeform_facts("user-1", [{}, {"name": "  ", "value": ""}])

    assert ctx == {}
    assert refusals == []


def test_fact_text_truncated(monkeypatch):
    parsed = ParsedFacts(decisions=[_LLMDecision(index=1, action="refused", reason="r")])
    _patch_llm(monkeypatch, parsed)

    long_fact = {"name": "n" * 1000, "value": "v" * 1000}
    _, refusals = parse_freeform_facts("user-1", [long_fact])

    # Names in refusals come from the TRUNCATED fact, not the raw input.
    assert len(refusals[0]["name"]) == context_llm.MAX_FACT_CHARS

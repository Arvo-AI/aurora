"""Prompt-cache breakpoints: prompt split, wire formats and the Bedrock request shape.

Three layers, no network anywhere:

1. ``assemble_system_prompt_parts`` — the static prefix holds only the three
   mode-level segments (in a fixed order, byte-identical across calls) and every
   per-org / per-incident / time-dependent segment lands in the dynamic suffix.
2. ``build_cached_system_prompt`` — Converse ``cachePoint`` blocks on Bedrock
   native, Anthropic ``cache_control`` on the direct / proxy paths, a plain string
   everywhere else and under the per-model cacheable minimum.
3. The request langchain-aws actually builds (``_messages_to_bedrock``,
   ``_converse_params``, ``_apply_cache_points``, and a full ``invoke`` against a
   fake boto3 client): cache points after the tools, after the static system text
   and at the end of the last message, never more than Bedrock's four.

Layer 3 and the middleware test need the real ``langchain_aws`` / ``langchain``
packages; CI's minimal dependency set stubs both, so those tests skip there and run
in the full local venv.
"""
from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from chat.backend.agent.prompt.composer import (
    assemble_system_prompt,
    assemble_system_prompt_parts,
)
from chat.backend.agent.prompt.schema import PromptSegments
from chat.backend.agent.utils import cache_control
from chat.backend.agent.utils.cache_control import (
    ANTHROPIC_CACHE_CONTROL,
    BEDROCK_CACHE_POINT,
    build_cached_system_prompt,
    is_claude_model,
    min_cacheable_chars,
)

HAIKU_55 = "anthropic/claude-haiku-5.5"
HAIKU_55_BEDROCK = "bedrock/eu.anthropic.claude-haiku-5-5"
OPUS_55 = "anthropic/claude-opus-5.5"
HAIKU_45 = "anthropic/claude-haiku-4.5"

# Comfortably above the 512-token minimum of the Claude 5.x generation
# (512 * 4 = 2,048 chars) and below Haiku 4.5's 4,096-token one (16,384 chars).
STATIC_3K = ("You are Aurora, an SRE copilot. Follow every rule below. " * 60)[:3000]
DYNAMIC = "SECURITY POLICY: deny rm -rf. RELEVANT MEMORIES: ... Last updated 12d ago."

# The real background-RCA static prefix measures ~11.4K chars: under Haiku 4.5's
# 16,384-char gate on its own, over it once the RCA dynamic suffix (memory
# contract, templates, rendered skills) is appended.
STATIC_11K = ("You are Aurora, an SRE copilot. Follow every rule below. " * 220)[:11400]
DYNAMIC_8K = ("BACKGROUND: alert payments-api in prod. MEMORY CONTRACT ... " * 150)[:8000]
assert len(STATIC_11K) < 4096 * 4 < len(STATIC_11K) + 1 + len(DYNAMIC_8K)


_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_PROMPT_CACHE_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "middleware", "prompt_cache.py"
)


def _real_module(name: str):
    """Import ``name`` and skip when it is absent or replaced by a conftest stub."""
    module = pytest.importorskip(name)
    if not getattr(module, "__file__", None):
        pytest.skip(f"{name} is stubbed in this environment")
    return module


def _load_prompt_cache_middleware():
    """Load the middleware module by path.

    Importing it through ``chat.backend.agent.middleware`` would run the package
    ``__init__``, whose ``context_trim`` import reaches ``chat.background`` (Redis at
    import time). The module itself only needs the real ``langchain``.
    """
    _real_module("langchain.agents.middleware.types")
    spec = importlib.util.spec_from_file_location("_prompt_cache_under_test", _PROMPT_CACHE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.BedrockPromptCacheMiddleware


def _is_cache_point(block) -> bool:
    return isinstance(block, dict) and block.get("cachePoint", {}).get("type") == "default"


# ---------------------------------------------------------------------------
# 1. Prompt split
# ---------------------------------------------------------------------------


def _segments(**overrides) -> PromptSegments:
    base = dict(
        system_invariant="INVARIANT: core rules",
        provider_constraints="CONSTRAINTS: aws only",
        regional_rules="REGIONS: honour geography",
        ephemeral_rules="MODE: AGENT",
        long_documents_note="LONG DOCUMENTS: zip referenced",
        provider_context="PROVIDER CONTEXT: aws account 123",
        prerequisite_checks="PREREQS: check gcloud",
        terraform_validation="TERRAFORM: validate",
        model_overlay="MODEL OVERLAY: opus",
        failure_recovery="FAILURE RECOVERY: retry",
        manual_vm_access="VM ACCESS: key hints",
        background_mode="BACKGROUND: alert payments-api in prod, deployments [a, b]",
        knowledge_base_memory="MEMORY INDEX\nRELEVANT MEMORIES ... Last updated 12d ago",
        integration_index="INTEGRATIONS: datadog, grafana",
        security_policy="POLICY: deny rm",
        is_rca_background=False,
    )
    base.update(overrides)
    return PromptSegments(**base)


class TestAssembleSystemPromptParts:
    def test_static_part_is_exactly_the_three_mode_level_segments(self):
        static, _ = assemble_system_prompt_parts(_segments())
        assert static == "INVARIANT: core rules\nCONSTRAINTS: aws only\nREGIONS: honour geography"

    def test_static_part_carries_no_per_org_incident_or_time_dependent_text(self):
        static, dynamic = assemble_system_prompt_parts(_segments())
        for marker in (
            "POLICY", "BACKGROUND", "MEMORY", "Last updated", "MODE: AGENT", "MODEL OVERLAY",
            "PROVIDER CONTEXT", "VM ACCESS", "INTEGRATIONS", "PREREQS", "LONG DOCUMENTS",
            "TERRAFORM", "FAILURE RECOVERY", "REMINDER",
        ):
            assert marker not in static, marker
            assert marker in dynamic, marker

    def test_dynamic_part_keeps_the_pre_caching_relative_order(self):
        _, dynamic = assemble_system_prompt_parts(_segments())
        expected = [
            "POLICY: deny rm",
            "BACKGROUND: alert payments-api in prod, deployments [a, b]",
            "MEMORY INDEX\nRELEVANT MEMORIES ... Last updated 12d ago",
            "MODE: AGENT",
            "MODEL OVERLAY: opus",
            "PROVIDER CONTEXT: aws account 123",
            "VM ACCESS: key hints",
            "INTEGRATIONS: datadog, grafana",
            "PREREQS: check gcloud",
            "LONG DOCUMENTS: zip referenced",
            "TERRAFORM: validate",
            "FAILURE RECOVERY: retry",
            "REMINDER: Commands that violate the organization policy will be rejected. "
            "Do not attempt workarounds.",
        ]
        assert dynamic == "\n".join(expected)

    def test_rca_background_drops_terraform_and_failure_recovery(self):
        _, dynamic = assemble_system_prompt_parts(_segments(is_rca_background=True))
        assert "TERRAFORM" not in dynamic
        assert "FAILURE RECOVERY" not in dynamic
        assert "BACKGROUND: alert" in dynamic

    def test_policy_reminder_only_with_a_security_policy(self):
        _, dynamic = assemble_system_prompt_parts(_segments(security_policy=""))
        assert "REMINDER" not in dynamic

    def test_empty_optional_segments_are_skipped(self):
        static, dynamic = assemble_system_prompt_parts(
            _segments(
                security_policy="", background_mode="", knowledge_base_memory="",
                integration_index="", manual_vm_access="", long_documents_note="",
                provider_context="", prerequisite_checks="", terraform_validation="",
                model_overlay="", failure_recovery="",
            )
        )
        assert static == "INVARIANT: core rules\nCONSTRAINTS: aws only\nREGIONS: honour geography"
        assert dynamic == "MODE: AGENT"

    def test_full_prompt_is_static_then_dynamic(self):
        segments = _segments()
        static, dynamic = assemble_system_prompt_parts(segments)
        assert assemble_system_prompt(segments) == static + "\n" + dynamic
        assert assemble_system_prompt(segments).startswith("INVARIANT: core rules")

    def test_static_part_is_deterministic(self):
        """Two builds from identical segments are byte-identical (no clock, no set order)."""
        first, _ = assemble_system_prompt_parts(_segments())
        second, _ = assemble_system_prompt_parts(_segments())
        assert first == second
        assert first.encode("utf-8") == second.encode("utf-8")


# ---------------------------------------------------------------------------
# 2. Wire formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [HAIKU_55, HAIKU_55_BEDROCK, OPUS_55, "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0", "claude-sonnet-4-6"],
)
def test_is_claude_model_accepts_every_spelling(model_id):
    assert is_claude_model(model_id)


@pytest.mark.parametrize("model_id", ["google/gemini-3.1-pro", "openai/gpt-5.6-sol", "bedrock/amazon.nova-pro-v1:0", "", None])
def test_is_claude_model_rejects_other_vendors(model_id):
    assert not is_claude_model(model_id)


def test_min_cacheable_chars_follows_the_per_model_minimum():
    assert min_cacheable_chars(HAIKU_55) == 512 * 4
    assert min_cacheable_chars("anthropic/claude-sonnet-5") == 1024 * 4
    assert min_cacheable_chars(HAIKU_45) == 4096 * 4


class TestBedrockNativeSystemBlocks:
    def test_system_is_static_cachepoint_dynamic(self):
        msg = build_cached_system_prompt(
            HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        assert isinstance(msg, SystemMessage)
        assert msg.content == [
            {"text": STATIC_3K},
            {"cachePoint": {"type": "default"}},
            {"text": DYNAMIC},
        ]
        # Raw Converse blocks: no "type" key, so langchain-aws passes them through.
        assert all("type" not in block for block in msg.content)
        assert msg.content[1] == BEDROCK_CACHE_POINT

    def test_explicit_bedrock_id_is_cached_too(self):
        msg = build_cached_system_prompt(
            HAIKU_55_BEDROCK, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        assert isinstance(msg, SystemMessage)
        assert _is_cache_point(msg.content[1])

    def test_no_dynamic_text_ends_with_the_cache_point(self):
        msg = build_cached_system_prompt(
            OPUS_55, STATIC_3K, "", serving_provider="bedrock", bedrock_native=True
        )
        assert msg.content == [{"text": STATIC_3K}, {"cachePoint": {"type": "default"}}]

    def test_below_the_model_minimum_stays_a_plain_string(self):
        """3,000 chars is under Haiku 4.5's 4,096-token (16,384-char) minimum."""
        prompt = build_cached_system_prompt(
            HAIKU_45, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        assert prompt == STATIC_3K + "\n" + DYNAMIC

    def test_short_static_with_long_whole_prompt_leaves_the_system_point_to_the_library(self):
        """Native mode never needs the single joined breakpoint: with a plain string
        ``_apply_cache_points`` (driven by BedrockPromptCacheMiddleware) appends its own
        system-end cachePoint, so the whole-system read is kept on 4,096-token models."""
        prompt = build_cached_system_prompt(
            HAIKU_45, STATIC_11K, DYNAMIC_8K, serving_provider="bedrock", bedrock_native=True
        )
        assert prompt == STATIC_11K + "\n" + DYNAMIC_8K

    def test_same_prefix_is_cached_on_a_512_token_model(self):
        assert isinstance(
            build_cached_system_prompt(HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True),
            SystemMessage,
        )

    def test_non_claude_model_on_bedrock_is_untouched(self):
        prompt = build_cached_system_prompt(
            "bedrock/amazon.nova-pro-v1:0", STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        assert prompt == STATIC_3K + "\n" + DYNAMIC

    def test_empty_static_text_is_untouched(self):
        assert build_cached_system_prompt(HAIKU_55, "", DYNAMIC, serving_provider="bedrock", bedrock_native=True) == DYNAMIC


class TestAnthropicStyleBlocks:
    @pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
    def test_direct_and_openrouter_get_block_level_cache_control(self, provider):
        msg = build_cached_system_prompt(OPUS_55, STATIC_3K, DYNAMIC, serving_provider=provider)
        assert isinstance(msg, SystemMessage)
        assert msg.content == [
            {"type": "text", "text": STATIC_3K, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": DYNAMIC, "cache_control": {"type": "ephemeral"}},
        ]
        assert msg.content[0]["cache_control"] == ANTHROPIC_CACHE_CONTROL

    def test_bedrock_gateway_keeps_the_markers_only_for_anthropic_prefixed_ids(self):
        """The OpenAI-shaped gateway received cache_control blocks for anthropic/ ids
        before the static/dynamic split (LLM_PROVIDER_MODE=bedrock + BEDROCK_BASE_URL);
        that shape is preserved, without any Converse cachePoint."""
        msg = build_cached_system_prompt(
            HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=False
        )
        assert isinstance(msg, SystemMessage)
        assert msg.content[0]["cache_control"] == {"type": "ephemeral"}
        assert all(block.get("type") == "text" for block in msg.content)
        assert not any(_is_cache_point(block) for block in msg.content)

    def test_bedrock_gateway_sends_a_plain_string_for_explicit_bedrock_ids(self):
        """The documented gateway setup (LLM_PROVIDER_MODE=direct +
        MAIN_MODEL=bedrock/eu.anthropic.claude-haiku-5-5) got a plain string before
        and keeps it: an OpenAI-shaped gateway caches through its own switches and may
        reject list-form system content."""
        prompt = build_cached_system_prompt(
            HAIKU_55_BEDROCK, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=False
        )
        assert prompt == STATIC_3K + "\n" + DYNAMIC
        assert isinstance(prompt, str)

    def test_bedrock_gateway_plain_string_for_explicit_ids_even_when_the_whole_prompt_is_large(self):
        prompt = build_cached_system_prompt(
            "bedrock/eu.anthropic.claude-opus-5-5", STATIC_11K, DYNAMIC_8K,
            serving_provider="bedrock", bedrock_native=False,
        )
        assert prompt == STATIC_11K + "\n" + DYNAMIC_8K

    def test_no_dynamic_text_single_block(self):
        msg = build_cached_system_prompt(OPUS_55, STATIC_3K, serving_provider="anthropic")
        assert msg.content == [
            {"type": "text", "text": STATIC_3K, "cache_control": {"type": "ephemeral"}}
        ]

    def test_below_the_minimum_stays_a_plain_string(self):
        """Static AND whole prompt under Haiku 4.5's 16,384-char minimum → nothing to cache."""
        prompt = build_cached_system_prompt(HAIKU_45, STATIC_3K, DYNAMIC, serving_provider="anthropic")
        assert prompt == STATIC_3K + "\n" + DYNAMIC

    @pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
    def test_short_static_but_long_whole_prompt_keeps_the_single_end_of_system_breakpoint(self, provider):
        """Background RCA on a 4,096-token-minimum model (RCA_MODEL=anthropic/claude-haiku-4.5):
        the 11.4K-char static prefix cannot be cached alone, but the whole prompt
        (>16,384 chars) could before the split and still is — exactly one cache_control,
        on the last (only) block, covering static + dynamic."""
        msg = build_cached_system_prompt(HAIKU_45, STATIC_11K, DYNAMIC_8K, serving_provider=provider)
        assert isinstance(msg, SystemMessage)
        assert len(msg.content) == 1
        block = msg.content[-1]
        assert block["type"] == "text"
        assert block["text"] == STATIC_11K + "\n" + DYNAMIC_8K
        assert block["cache_control"] == {"type": "ephemeral"}
        assert sum("cache_control" in b for b in msg.content) == 1

    def test_single_breakpoint_shape_matches_the_pre_split_wire_format(self):
        """Byte-for-byte the SystemMessage the pre-split code produced for the same prompt."""
        whole = STATIC_11K + "\n" + DYNAMIC_8K
        msg = build_cached_system_prompt(HAIKU_45, STATIC_11K, DYNAMIC_8K, serving_provider="anthropic")
        assert msg.content == [{"type": "text", "text": whole, "cache_control": {"type": "ephemeral"}}]

    def test_bedrock_gateway_anthropic_id_also_keeps_the_single_breakpoint(self):
        msg = build_cached_system_prompt(
            HAIKU_45, STATIC_11K, DYNAMIC_8K, serving_provider="bedrock", bedrock_native=False
        )
        assert isinstance(msg, SystemMessage)
        assert len(msg.content) == 1
        assert msg.content[0]["cache_control"] == {"type": "ephemeral"}

    def test_static_that_qualifies_on_its_own_keeps_two_breakpoints(self):
        """Same 11.4K-char prefix on a 512-token model: the two-breakpoint layout."""
        msg = build_cached_system_prompt(HAIKU_55, STATIC_11K, DYNAMIC_8K, serving_provider="anthropic")
        assert [b["text"] for b in msg.content] == [STATIC_11K, DYNAMIC_8K]
        assert all(b["cache_control"] == {"type": "ephemeral"} for b in msg.content)


class TestPlainStringPaths:
    @pytest.mark.parametrize("provider", ["google", "openai", "vertex", "ollama", "", None])
    def test_other_providers_get_the_plain_prompt(self, provider):
        prompt = build_cached_system_prompt(OPUS_55, STATIC_3K, DYNAMIC, serving_provider=provider)
        assert prompt == STATIC_3K + "\n" + DYNAMIC

    def test_non_claude_models_get_the_plain_prompt(self):
        prompt = build_cached_system_prompt("google/gemini-3.1-pro", STATIC_3K, DYNAMIC, serving_provider="google")
        assert prompt == STATIC_3K + "\n" + DYNAMIC

    def test_failure_falls_back_to_the_plain_prompt(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("message construction failed")

        monkeypatch.setattr(cache_control, "SystemMessage", boom)
        prompt = build_cached_system_prompt(
            HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        assert prompt == STATIC_3K + "\n" + DYNAMIC


# ---------------------------------------------------------------------------
# 3. Request shape built by langchain-aws (real library, fake boto3 client)
# ---------------------------------------------------------------------------


def _bedrock_converse_module():
    return _real_module("langchain_aws.chat_models.bedrock_converse")


def _haiku_model(bc):
    # Dummy static credentials: the boto3 client is built locally and never called.
    return bc.ChatBedrockConverse(
        model="eu.anthropic.claude-haiku-5-5",
        region_name="eu-central-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _dummy_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "cloud_exec",
                "description": "Run a read-only cloud CLI command.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load an integration skill.",
                "parameters": {
                    "type": "object",
                    "properties": {"skill_id": {"type": "string"}},
                    "required": ["skill_id"],
                },
            },
        },
    ]


def _conversation(system_prompt):
    return [
        system_prompt if isinstance(system_prompt, SystemMessage) else SystemMessage(content=system_prompt),
        HumanMessage(content="Investigate the payments-api alert."),
        AIMessage(content="I will start by checking the pods."),
        HumanMessage(content="Go ahead."),
    ]


class TestBedrockRequestShape:
    def test_cache_points_after_tools_static_system_and_last_message(self):
        bc = _bedrock_converse_module()
        model = _haiku_model(bc)
        system_prompt = build_cached_system_prompt(
            HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        bedrock_messages, system = bc._messages_to_bedrock(
            _conversation(system_prompt), None, model_id=model._get_base_model(), provider="anthropic"
        )
        params = model._converse_params(tools=_dummy_tools())
        model._apply_cache_points({"type": "ephemeral"}, system, bedrock_messages, params)

        # System: static text, cache point, dynamic text — the manual point is kept and
        # the library does not add a second one at the end of the system list.
        assert system == [
            {"text": STATIC_3K},
            {"cachePoint": {"type": "default"}},
            {"text": DYNAMIC},
        ]
        # Tools: cache point appended after the last tool definition.
        tools = params["toolConfig"]["tools"]
        assert len(tools) == 3
        assert all("toolSpec" in t for t in tools[:2])
        assert _is_cache_point(tools[-1])
        # Messages: the last message ends with a cache point; nothing on earlier turns.
        assert _is_cache_point(bedrock_messages[-1]["content"][-1])
        assert not any(_is_cache_point(b) for b in bedrock_messages[-2]["content"])
        assert not any(_is_cache_point(b) for b in bedrock_messages[0]["content"])
        # Budget: 3 points, under Bedrock's maximum of 4.
        assert bc._count_cache_points(system, bedrock_messages, tools) == 3 <= bc._MAX_CACHE_POINTS

    def test_ttl_is_the_5_minute_default(self):
        bc = _bedrock_converse_module()
        model = _haiku_model(bc)
        system_prompt = build_cached_system_prompt(
            HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        bedrock_messages, system = bc._messages_to_bedrock(
            _conversation(system_prompt), None, model_id=model._get_base_model(), provider="anthropic"
        )
        params = model._converse_params(tools=_dummy_tools())
        model._apply_cache_points({"type": "ephemeral"}, system, bedrock_messages, params)
        for block in (system[1], params["toolConfig"]["tools"][-1], bedrock_messages[-1]["content"][-1]):
            assert block == {"cachePoint": {"type": "default"}}
            assert "ttl" not in block["cachePoint"]

    def test_plain_string_prompt_never_exceeds_four_points(self):
        """Under the cacheable minimum the system is a plain string; the library then
        places its own points (system end, last message, tools, end of history)."""
        bc = _bedrock_converse_module()
        model = _haiku_model(bc)
        bedrock_messages, system = bc._messages_to_bedrock(
            _conversation("short prompt"), None, model_id=model._get_base_model(), provider="anthropic"
        )
        params = model._converse_params(tools=_dummy_tools())
        model._apply_cache_points({"type": "ephemeral"}, system, bedrock_messages, params)
        assert bc._count_cache_points(system, bedrock_messages, params["toolConfig"]["tools"]) <= bc._MAX_CACHE_POINTS

    def test_haiku_45_rca_prompt_still_gets_a_system_end_point_from_the_library(self):
        """RCA_MODEL pinned to Haiku 4.5 in native mode: the 11.4K-char static prefix is
        under the 4,096-token minimum, so Aurora sends the plain string and the
        library's own system-end cachePoint covers the whole (>16K-char) system
        prompt — no cache regression versus the pre-split single breakpoint."""
        bc = _bedrock_converse_module()
        model = bc.ChatBedrockConverse(
            model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            region_name="eu-central-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        system_prompt = build_cached_system_prompt(
            HAIKU_45, STATIC_11K, DYNAMIC_8K, serving_provider="bedrock", bedrock_native=True
        )
        assert isinstance(system_prompt, str)
        bedrock_messages, system = bc._messages_to_bedrock(
            _conversation(system_prompt), None, model_id=model._get_base_model(), provider="anthropic"
        )
        params = model._converse_params(tools=_dummy_tools())
        model._apply_cache_points({"type": "ephemeral"}, system, bedrock_messages, params)
        assert system[0] == {"text": STATIC_11K + "\n" + DYNAMIC_8K}
        assert _is_cache_point(system[-1])
        assert _is_cache_point(params["toolConfig"]["tools"][-1])
        assert _is_cache_point(bedrock_messages[-1]["content"][-1])
        assert bc._count_cache_points(system, bedrock_messages, params["toolConfig"]["tools"]) <= bc._MAX_CACHE_POINTS

    def test_anthropic_style_blocks_would_be_dropped_on_bedrock(self):
        """Why the native path needs raw blocks: cache_control on a typed text block
        is stripped by _lc_content_to_bedrock (langchain-aws 1.9.0)."""
        bc = _bedrock_converse_module()
        blocks = bc._lc_content_to_bedrock(
            [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]
        )
        assert blocks == [{"text": "x"}]

    def test_invoke_sends_the_cache_points_through_bind_tools(self):
        """End to end through create_agent's binding: ``bind_tools(..., cache_control=...)``
        (what ``model_settings`` becomes) + the cached SystemMessage, against a fake
        boto3 client that records the Converse request."""
        bc = _bedrock_converse_module()
        model = _haiku_model(bc)
        captured = {}

        def fake_converse(**request):
            captured.update(request)
            return {
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 1, "totalTokens": 11},
                "metrics": {"latencyMs": 1},
            }

        model.client = SimpleNamespace(converse=fake_converse)
        system_prompt = build_cached_system_prompt(
            HAIKU_55, STATIC_3K, DYNAMIC, serving_provider="bedrock", bedrock_native=True
        )
        bound = model.bind_tools(_dummy_tools(), tool_choice=None, cache_control={"type": "ephemeral"})
        bound.invoke(_conversation(system_prompt))

        assert captured["modelId"] == "eu.anthropic.claude-haiku-5-5"
        assert captured["system"] == [
            {"text": STATIC_3K},
            {"cachePoint": {"type": "default"}},
            {"text": DYNAMIC},
        ]
        tools = captured["toolConfig"]["tools"]
        assert _is_cache_point(tools[-1])
        assert len(tools) == 3
        assert _is_cache_point(captured["messages"][-1]["content"][-1])
        assert bc._count_cache_points(captured["system"], captured["messages"], tools) == 3
        # cache_control itself never leaks into the request body.
        assert "cache_control" not in captured
        assert "cacheControl" not in captured



# ---------------------------------------------------------------------------
# Middleware: model_settings carries cache_control on every turn
# ---------------------------------------------------------------------------


class TestBedrockPromptCacheMiddleware:
    @staticmethod
    def _request(**kwargs):
        types_module = _real_module("langchain.agents.middleware.types")
        model = MagicMock(name="chat_model")
        return types_module.ModelRequest(model=model, messages=[HumanMessage(content="x")], **kwargs)

    def test_adds_cache_control_and_keeps_other_fields(self):
        middleware_cls = _load_prompt_cache_middleware()
        request = self._request(tool_choice="auto", model_settings={"existing": 1})
        seen = {}

        def handler(req):
            seen["request"] = req
            return "response"

        assert middleware_cls().wrap_model_call(request, handler) == "response"
        patched = seen["request"]
        assert patched.model_settings == {"existing": 1, "cache_control": {"type": "ephemeral"}}
        assert patched.tool_choice == "auto"
        assert patched.messages == request.messages
        assert patched.model is request.model
        # The original request is not mutated (override returns a copy).
        assert request.model_settings == {"existing": 1}

    def test_async_path_matches(self):
        import asyncio

        middleware_cls = _load_prompt_cache_middleware()
        request = self._request()
        seen = {}

        async def handler(req):
            seen["request"] = req
            return "response"

        result = asyncio.run(middleware_cls().awrap_model_call(request, handler))
        assert result == "response"
        assert seen["request"].model_settings == {"cache_control": {"type": "ephemeral"}}

    def test_explicit_cache_control_is_not_overridden(self):
        middleware_cls = _load_prompt_cache_middleware()
        request = self._request(model_settings={"cache_control": {"type": "ephemeral", "ttl": "1h"}})
        seen = {}
        middleware_cls().wrap_model_call(request, lambda req: seen.setdefault("request", req))
        assert seen["request"].model_settings == {"cache_control": {"type": "ephemeral", "ttl": "1h"}}

    def test_one_hour_ttl_is_forwarded(self):
        middleware_cls = _load_prompt_cache_middleware()
        request = self._request()
        seen = {}
        middleware_cls(ttl="1h").wrap_model_call(request, lambda req: seen.setdefault("request", req))
        assert seen["request"].model_settings == {"cache_control": {"type": "ephemeral", "ttl": "1h"}}

    def test_force_tool_choice_override_preserves_the_cache_setting(self):
        """ForceToolChoice runs outermost and calls request.override(tool_choice=...);
        the cache setting added afterwards must survive, and vice versa."""
        middleware_cls = _load_prompt_cache_middleware()
        request = self._request().override(tool_choice={"tool": {"name": "trigger_rca"}})
        seen = {}
        middleware_cls().wrap_model_call(request, lambda req: seen.setdefault("request", req))
        patched = seen["request"]
        assert patched.tool_choice == {"tool": {"name": "trigger_rca"}}
        assert patched.model_settings == {"cache_control": {"type": "ephemeral"}}
        assert patched.override(tool_choice="auto").model_settings == {"cache_control": {"type": "ephemeral"}}

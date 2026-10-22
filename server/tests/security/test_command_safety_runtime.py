"""Tests the LLM-based command safety judge -- the last guardrail layer
before a command runs against user infrastructure. Pins fail-closed
behaviour on every runtime error path (judge timeout, LLM exception,
malformed JSON verdict, missing user message): each must block the
command, never let it through. The only legitimate skip path is when
the safety config itself is disabled. Also covers the ``_fingerprint``
helper used to deduplicate verdicts in audit logs. Static prompt
coverage lives in ``test_prompt_injection.py``; this file is the live
evaluation surface.
"""

import concurrent.futures
import os
import sys
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.security import command_safety  # noqa: E402
from utils.security.command_safety import (  # noqa: E402
    SafetyVerdict,
    _fingerprint,
    _trim_user_message,
    check_command_safety,
)

_HEX_ALPHABET = set("0123456789abcdef")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_verdict_cache(monkeypatch):
    """Every test starts with an empty in-process verdict cache and no Redis, so a
    verdict remembered by one test can never leak into the next."""
    monkeypatch.setattr(command_safety, "_redis_client", lambda: None)
    monkeypatch.delenv("COMMAND_SAFETY_VERDICT_TTL_SECONDS", raising=False)
    command_safety._clear_verdict_cache()
    yield
    command_safety._clear_verdict_cache()


def _patch_config(monkeypatch, *, enabled: bool) -> None:
    monkeypatch.setattr(command_safety, "config", MagicMock(enabled=enabled))


def _patch_user_message(monkeypatch, msg) -> None:
    monkeypatch.setattr(
        command_safety,
        "_get_latest_user_message",
        MagicMock(return_value=msg),
    )


def _forbid_llm(monkeypatch) -> MagicMock:
    spy = MagicMock(side_effect=AssertionError("LLM must not run"))
    monkeypatch.setattr(command_safety, "_call_llm", spy)
    return spy


# ---------------------------------------------------------------------------
# Disabled config -- the only legitimate let-through path
# ---------------------------------------------------------------------------


class TestDisabledConfig:
    def test_disabled_returns_safe_verdict_without_llm(self, monkeypatch):
        _patch_config(monkeypatch, enabled=False)
        spy = _forbid_llm(monkeypatch)

        v = check_command_safety("rm -rf /")

        assert v.conclusion is False
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Missing user context -- fail closed
# ---------------------------------------------------------------------------


class TestMissingUserContext:
    """No user message means no intent to reason against; block."""

    @pytest.mark.parametrize("msg", [None, ""])
    def test_falsy_user_message_blocks_without_calling_llm(
        self, msg, monkeypatch,
    ):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, msg)
        spy = _forbid_llm(monkeypatch)

        v = check_command_safety("ls -la")

        assert v.conclusion is True
        assert v.observation == "error"
        assert "missing user context" in v.thought
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# LLM call failures -- timeout / exception / malformed payload
# ---------------------------------------------------------------------------


class TestTimeoutBlocks:
    """``concurrent.futures.TimeoutError`` -> blocking verdict."""

    def test_timeout_returns_blocking_verdict(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "Check disk usage")
        monkeypatch.setattr(
            command_safety,
            "_call_llm",
            MagicMock(side_effect=concurrent.futures.TimeoutError()),
        )

        v = check_command_safety("df -h")

        assert v.conclusion is True
        assert v.observation == "error"
        assert "timeout" in v.thought.lower()
        assert "failing closed" in v.thought.lower()


class TestLlmRaisesBlocks:
    """Any non-timeout exception from ``_call_llm`` must fail closed."""

    @pytest.mark.parametrize("exc", [
        RuntimeError("provider 503"),
        ConnectionError("dns down"),
        ValueError("bad request"),
        OSError("socket reset"),
    ])
    def test_exception_returns_blocking_verdict(self, exc, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "List my pods")
        monkeypatch.setattr(
            command_safety, "_call_llm", MagicMock(side_effect=exc),
        )

        v = check_command_safety("kubectl get pods")

        assert v.conclusion is True
        assert v.observation == "error"
        assert "failing closed" in v.thought.lower()


class TestMalformedPayloadBlocks:
    """Pydantic ``ValidationError`` is the realistic shape of "malformed
    JSON" in production -- ``with_structured_output`` raises it when the
    model returns JSON that doesn't fit ``SafetyVerdict``."""

    def test_pydantic_validation_error_blocks(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "Show files")

        try:
            SafetyVerdict.model_validate({"observation": "missing fields"})
        except ValidationError as ve:
            real_validation_error = ve
        else:
            pytest.fail("SafetyVerdict.model_validate should reject this dict")

        monkeypatch.setattr(
            command_safety,
            "_call_llm",
            MagicMock(side_effect=real_validation_error),
        )

        v = check_command_safety("ls")

        assert v.conclusion is True
        assert v.observation == "error"


# ---------------------------------------------------------------------------
# Clean LLM verdicts -- pin allow vs block when nothing fails
# ---------------------------------------------------------------------------


class TestLlmReturnsCleanVerdict:
    """When the LLM responds normally, its verdict is what the caller sees."""

    def test_safe_verdict_passes_through_unchanged(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "List my pods")
        safe = SafetyVerdict(
            observation="kubectl read",
            thought="read-only diagnostic",
            conclusion=False,
        )
        monkeypatch.setattr(
            command_safety, "_call_llm", MagicMock(return_value=safe),
        )

        v = check_command_safety("kubectl get pods")

        assert v is safe
        assert v.conclusion is False

    def test_dangerous_verdict_blocks(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "Clean up test pods")
        bad = SafetyVerdict(
            observation="rm root", thought="destructive", conclusion=True,
        )
        monkeypatch.setattr(
            command_safety, "_call_llm", MagicMock(return_value=bad),
        )

        v = check_command_safety("rm -rf /")

        assert v.conclusion is True
        assert v.observation == "rm root"


# ---------------------------------------------------------------------------
# _fingerprint -- stable, length 16, hex-only, robust to bad input
# ---------------------------------------------------------------------------


class TestFingerprintShape:
    """Length 16 / lowercase hex -- log greps and SIEM rules depend on it."""

    @pytest.mark.parametrize("cmd", [
        "kubectl get pods",
        "aws ec2 describe-instances",
        "rm -rf /tmp/foo",
        "",
        " ",
    ])
    def test_length_is_always_16(self, cmd):
        assert len(_fingerprint(cmd)) == 16

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "echo hello",
        "kubectl describe pod foo",
        "",
    ])
    def test_only_lowercase_hex_characters(self, cmd):
        assert set(_fingerprint(cmd)) <= _HEX_ALPHABET


class TestFingerprintDeterminism:
    def test_stable_across_calls(self):
        cmd = "rm -rf /tmp/foo"
        assert _fingerprint(cmd) == _fingerprint(cmd)

    def test_different_inputs_yield_different_fingerprints(self):
        assert _fingerprint("ls -la") != _fingerprint("ls -lA")

    def test_distinguishes_empty_from_whitespace(self):
        assert _fingerprint("") != _fingerprint(" ")


class TestFingerprintRobustness:
    """``errors='replace'`` keeps the helper non-throwing on adversarial input."""

    def test_unicode_input_does_not_raise(self):
        fp = _fingerprint("echo 你好")
        assert len(fp) == 16
        assert set(fp) <= _HEX_ALPHABET

    def test_lone_surrogate_does_not_raise(self):
        fp = _fingerprint("\ud800")
        assert len(fp) == 16
        assert set(fp) <= _HEX_ALPHABET

    def test_null_byte_does_not_raise(self):
        fp = _fingerprint("ls\x00 -la")
        assert len(fp) == 16
        assert set(fp) <= _HEX_ALPHABET

    def test_long_input_still_returns_16_chars(self):
        fp = _fingerprint("a" * 100_000)
        assert len(fp) == 16
        assert set(fp) <= _HEX_ALPHABET


# ---------------------------------------------------------------------------
# evaluate_command pipeline -- signature matcher → LLM judge
# ---------------------------------------------------------------------------


def _silence_audit(monkeypatch) -> None:
    import utils.security.audit_events as audit_mod

    monkeypatch.setattr(audit_mod, "emit_block_event", MagicMock())


class TestEvaluateCommandPipeline:
    """``command_safety.evaluate_command`` chains the signature matcher and the
    LLM judge into one pipeline. The signature check runs first; if it blocks,
    the LLM judge is never called. If both pass, the command proceeds.

    Tests use real signature-matcher behaviour: ``rm -rf /`` is a known
    signature hit; ``uptime`` passes all signatures cleanly."""

    def test_disabled_config_returns_not_blocked(self, monkeypatch):
        _patch_config(monkeypatch, enabled=False)

        result = command_safety.evaluate_command("rm -rf /", tool="test")

        assert result.blocked is False

    def test_signature_match_blocks_without_calling_safety_check(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _silence_audit(monkeypatch)
        safety_spy = MagicMock()
        monkeypatch.setattr(command_safety, "check_command_safety", safety_spy)

        result = command_safety.evaluate_command("rm -rf /", tool="test")

        assert result.blocked is True
        assert result.layer == "signature_match"
        safety_spy.assert_not_called()

    def test_signature_miss_then_llm_judge_blocks(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _silence_audit(monkeypatch)
        bad = SafetyVerdict(
            observation="suspicious", thought="destructive operation", conclusion=True,
        )
        monkeypatch.setattr(
            command_safety, "check_command_safety", MagicMock(return_value=bad),
        )

        result = command_safety.evaluate_command("uptime", tool="test")

        assert result.blocked is True
        assert result.layer == "llm_judge"
        assert result.reason == "destructive operation"

    def test_both_layers_pass_returns_not_blocked(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        safe = SafetyVerdict(
            observation="system check", thought="benign diagnostic", conclusion=False,
        )
        monkeypatch.setattr(
            command_safety, "check_command_safety", MagicMock(return_value=safe),
        )

        result = command_safety.evaluate_command("uptime", tool="test")

        assert result.blocked is False
        assert result.layer == ""


# ---------------------------------------------------------------------------
# _trim_user_message -- the judge sees intent, not the whole alert payload
# ---------------------------------------------------------------------------


class TestTrimUserMessage:
    def test_short_message_is_untouched(self):
        msg = "Check why my server is slow"
        assert _trim_user_message(msg) is msg

    def test_message_at_threshold_is_untouched(self):
        msg = "title\n" + "x" * (command_safety._USER_MESSAGE_TRIM_THRESHOLD - 6)
        assert len(msg) == command_safety._USER_MESSAGE_TRIM_THRESHOLD
        assert _trim_user_message(msg) is msg

    def test_long_message_keeps_title_head_and_tail(self):
        title = "ALERT: payments-api 5xx rate above 5%"
        body = "H" * 10_000 + "M" * 40_000 + "T" * 10_000
        out = _trim_user_message(title + "\n" + body)

        assert out.startswith(title + "\n" + "H" * command_safety._USER_MESSAGE_HEAD_CHARS)
        assert out.endswith("T" * command_safety._USER_MESSAGE_TAIL_CHARS)
        assert "chars omitted" in out
        assert "M" not in out.replace("chars omitted", "")
        assert len(out) < 4_600

    def test_sixty_k_single_line_is_bounded(self):
        out = _trim_user_message("a" * 30_000 + "b" * 30_000)
        assert out.startswith("a" * command_safety._USER_MESSAGE_HEAD_CHARS)
        assert out.endswith("b" * command_safety._USER_MESSAGE_TAIL_CHARS)
        assert "56000 chars omitted" in out

    def test_oversized_first_line_is_capped_too(self):
        out = _trim_user_message("t" * 9_000 + "\n" + "r" * 9_000)
        assert len(out) < 7_000
        assert out.startswith("t" * command_safety._USER_MESSAGE_HEAD_CHARS + "\n")

    def test_none_passes_through(self):
        assert _trim_user_message(None) is None

    def test_judge_prompt_carries_the_trimmed_message_but_cache_key_uses_the_full_one(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        huge = "Investigate alert\n" + "p" * 60_000
        _patch_user_message(monkeypatch, huge)
        seen = {}

        def _fake_call(prompt, user_id, session_id):
            seen["prompt"] = prompt
            return SafetyVerdict(observation="o", thought="t", conclusion=False)

        monkeypatch.setattr(command_safety, "_call_llm", _fake_call)

        check_command_safety("kubectl get pods", session_id="s-1")

        assert len(seen["prompt"]) < 6_000
        assert "chars omitted" in seen["prompt"]
        # A different full message with the same trimmed shape must re-judge:
        # the key is the digest of the untrimmed text.
        key_a = command_safety._verdict_cache_key("s-1", huge, "kubectl get pods", "command_execution")
        key_b = command_safety._verdict_cache_key("s-1", huge[:-1] + "q", "kubectl get pods", "command_execution")
        assert key_a != key_b


# ---------------------------------------------------------------------------
# Verdict cache -- same (session, message, command) is judged once
# ---------------------------------------------------------------------------


class TestVerdictCache:
    def _spy_llm(self, monkeypatch, verdict):
        spy = MagicMock(return_value=verdict)
        monkeypatch.setattr(command_safety, "_call_llm", spy)
        return spy

    def test_second_identical_check_skips_the_llm(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "List my pods")
        safe = SafetyVerdict(observation="kubectl read", thought="ro", conclusion=False)
        spy = self._spy_llm(monkeypatch, safe)

        first = check_command_safety("kubectl get pods", session_id="s-1", user_id="u-1")
        second = check_command_safety("kubectl get pods", session_id="s-1", user_id="u-1")

        assert first is safe
        assert second.conclusion is False
        assert second.observation == "kubectl read"
        spy.assert_called_once()

    def test_blocking_verdicts_are_cached_too(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "Clean up")
        bad = SafetyVerdict(observation="rm root", thought="destructive", conclusion=True)
        spy = self._spy_llm(monkeypatch, bad)

        check_command_safety("rm -rf /var/lib/app", session_id="s-1")
        again = check_command_safety("rm -rf /var/lib/app", session_id="s-1")

        assert again.conclusion is True
        spy.assert_called_once()

    @pytest.mark.parametrize(
        ("second_session", "second_message", "second_command"),
        [
            ("s-2", "List my pods", "kubectl get pods"),
            ("s-1", "Delete my pods", "kubectl get pods"),
            ("s-1", "List my pods", "kubectl get pods -A"),
        ],
    )
    def test_any_key_component_change_re_judges(
        self, monkeypatch, second_session, second_message, second_command,
    ):
        _patch_config(monkeypatch, enabled=True)
        safe = SafetyVerdict(observation="o", thought="t", conclusion=False)
        spy = self._spy_llm(monkeypatch, safe)

        _patch_user_message(monkeypatch, "List my pods")
        check_command_safety("kubectl get pods", session_id="s-1")
        _patch_user_message(monkeypatch, second_message)
        check_command_safety(second_command, session_id=second_session)

        assert spy.call_count == 2

    def test_timeout_is_not_cached(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "Check disk usage")
        spy = MagicMock(side_effect=[
            concurrent.futures.TimeoutError(),
            SafetyVerdict(observation="df", thought="ro", conclusion=False),
        ])
        monkeypatch.setattr(command_safety, "_call_llm", spy)

        first = check_command_safety("df -h", session_id="s-1")
        second = check_command_safety("df -h", session_id="s-1")

        assert first.conclusion is True and first.observation == "error"
        assert second.conclusion is False
        assert spy.call_count == 2

    def test_llm_error_is_not_cached(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "Check disk usage")
        spy = MagicMock(side_effect=[
            RuntimeError("provider 503"),
            SafetyVerdict(observation="df", thought="ro", conclusion=False),
        ])
        monkeypatch.setattr(command_safety, "_call_llm", spy)

        check_command_safety("df -h", session_id="s-1")
        second = check_command_safety("df -h", session_id="s-1")

        assert second.conclusion is False
        assert spy.call_count == 2

    def test_ttl_zero_disables_the_cache(self, monkeypatch):
        monkeypatch.setenv("COMMAND_SAFETY_VERDICT_TTL_SECONDS", "0")
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "List my pods")
        spy = self._spy_llm(monkeypatch, SafetyVerdict(observation="o", thought="t", conclusion=False))

        check_command_safety("kubectl get pods", session_id="s-1")
        check_command_safety("kubectl get pods", session_id="s-1")

        assert spy.call_count == 2

    @pytest.mark.parametrize("raw", ["", "  ", "abc"])
    def test_junk_ttl_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv("COMMAND_SAFETY_VERDICT_TTL_SECONDS", raw)
        assert command_safety._verdict_ttl_seconds() == command_safety._VERDICT_TTL_DEFAULT

    def test_negative_ttl_disables(self, monkeypatch):
        monkeypatch.setenv("COMMAND_SAFETY_VERDICT_TTL_SECONDS", "-5")
        assert command_safety._verdict_ttl_seconds() == 0

    def test_cache_key_contains_only_digests(self):
        key = command_safety._verdict_cache_key("sess", "Delete the prod database now", "dropdb prod", "terminal_run")
        assert "Delete" not in key and "dropdb" not in key
        prefix, sess, tool, msg_digest, cmd_digest = key.rsplit(":", 4)
        assert prefix == "cs:verdict"
        assert sess == "sess"
        assert tool == "terminal_run"
        assert len(msg_digest) == 64 and set(msg_digest) <= _HEX_ALPHABET
        assert len(cmd_digest) == 64 and set(cmd_digest) <= _HEX_ALPHABET

    def test_tool_name_is_part_of_the_key(self, monkeypatch):
        """The tool is judge context, so the same command through another tool re-judges."""
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "List my pods")
        spy = self._spy_llm(monkeypatch, SafetyVerdict(observation="o", thought="t", conclusion=False))

        check_command_safety("kubectl get pods", tool_name="command_execution", session_id="s-1")
        check_command_safety("kubectl get pods", tool_name="kubectl_onprem", session_id="s-1")
        check_command_safety("kubectl get pods", tool_name="kubectl_onprem", session_id="s-1")

        assert spy.call_count == 2

    def test_redis_is_preferred_and_local_cache_is_the_fallback(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _patch_user_message(monkeypatch, "List my pods")
        store = {}
        redis = MagicMock()
        redis.get.side_effect = lambda k: store.get(k)
        redis.set.side_effect = lambda k, v, ex=None: store.__setitem__(k, v)
        monkeypatch.setattr(command_safety, "_redis_client", lambda: redis)
        spy = self._spy_llm(monkeypatch, SafetyVerdict(observation="o", thought="t", conclusion=False))

        check_command_safety("kubectl get pods", session_id="s-1")
        check_command_safety("kubectl get pods", session_id="s-1")

        spy.assert_called_once()
        assert len(store) == 1
        (key,) = store
        assert key.startswith("cs:verdict:s-1:command_execution:")
        assert redis.set.call_args.kwargs["ex"] == command_safety._VERDICT_TTL_DEFAULT
        # Nothing landed in the fallback cache while Redis was healthy.
        assert len(command_safety._fallback_verdict_cache) == 0


# ---------------------------------------------------------------------------
# Structured-output unwrapping + usage recording
# ---------------------------------------------------------------------------


class TestStructuredResultAndUsage:
    def test_include_raw_shape_yields_verdict_and_raw(self):
        raw = object()
        verdict = SafetyVerdict(observation="o", thought="t", conclusion=False)
        out, got_raw = command_safety._unwrap_structured_result(
            {"raw": raw, "parsed": verdict, "parsing_error": None}
        )
        assert out is verdict and got_raw is raw

    def test_parsing_failure_raises_so_the_caller_fails_closed(self):
        with pytest.raises(ValueError):
            command_safety._unwrap_structured_result(
                {"raw": object(), "parsed": None, "parsing_error": "bad json"}
            )

    def test_legacy_bare_verdict_still_accepted(self):
        verdict = SafetyVerdict(observation="o", thought="t", conclusion=True)
        out, raw = command_safety._unwrap_structured_result(verdict)
        assert out is verdict and raw is None

    def test_usage_is_recorded_under_the_resolved_model_with_the_raw_response(self, monkeypatch):
        import types

        tracker_mod = types.ModuleType("chat.backend.agent.utils.llm_usage_tracker")
        tracker_mod.LLMUsageTracker = MagicMock()
        llm_mod = types.ModuleType("chat.backend.agent.llm")
        llm_mod.ModelConfig = MagicMock(GUARDRAILS_MODEL="anthropic/claude-haiku-5.5")
        providers_mod = types.ModuleType("chat.backend.agent.providers")
        registry = MagicMock()
        registry.resolve_provider_name.return_value = "bedrock"
        providers_mod.get_registry = lambda: registry
        monkeypatch.setitem(sys.modules, "chat.backend.agent.utils.llm_usage_tracker", tracker_mod)
        monkeypatch.setitem(sys.modules, "chat.backend.agent.llm", llm_mod)
        monkeypatch.setitem(sys.modules, "chat.backend.agent.providers", providers_mod)
        monkeypatch.setattr(command_safety, "config", MagicMock(enabled=True, llm_model=""))
        monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
        raw_response = object()

        command_safety._track_usage("u-1", "s-1", [{"role": "user", "content": "x"}], 0.0, None, raw_response)

        kwargs = tracker_mod.LLMUsageTracker.track_llm_call.call_args.kwargs
        assert kwargs["model_name"] == "anthropic/claude-haiku-5.5"
        assert kwargs["api_provider"] == "bedrock"
        assert kwargs["response"] is raw_response
        assert kwargs["request_type"] == "command_safety"


# ---------------------------------------------------------------------------
# Judge model construction
# ---------------------------------------------------------------------------


class TestJudgeModelConstruction:
    def test_judge_asks_for_cap_thinking_off_and_a_bounded_client_timeout(self, monkeypatch):
        """The judge is built through the shared instance cache with the judge cap,
        thinking off and the client's own timeout bounded to ``_TIMEOUT_SECONDS``:
        ``future.result(timeout=...)`` cannot cancel the in-flight request, so
        without it a stalled provider would hold a pool thread for the full
        deployment-wide read timeout times the retry count. Whether the cap
        actually reaches a model that keeps thinking is the provider's call
        (``call_options.effective_max_tokens``), exercised in the provider tests."""
        import types

        from chat.backend.agent.call_options import max_tokens_for

        captured = {}

        def fake_get_cached_chat_model(model, temperature=0.0, provider_mode=None, **kwargs):
            captured["model"] = model
            captured["temperature"] = temperature
            captured.update(kwargs)
            base = MagicMock()
            base.with_structured_output.return_value = "structured-runnable"
            captured["base"] = base
            return base

        providers_mod = types.ModuleType("chat.backend.agent.providers")
        providers_mod.get_cached_chat_model = fake_get_cached_chat_model
        llm_mod = types.ModuleType("chat.backend.agent.llm")
        llm_mod.ModelConfig = MagicMock(GUARDRAILS_MODEL="anthropic/claude-haiku-5.5")
        monkeypatch.setitem(sys.modules, "chat.backend.agent.providers", providers_mod)
        monkeypatch.setitem(sys.modules, "chat.backend.agent.llm", llm_mod)
        monkeypatch.setattr(command_safety, "config", MagicMock(enabled=True, llm_model=""))
        monkeypatch.delenv("LLM_MAX_TOKENS_JUDGE", raising=False)
        command_safety._create_safety_llm.cache_clear()
        try:
            runnable = command_safety._create_safety_llm()
        finally:
            command_safety._create_safety_llm.cache_clear()

        assert runnable == "structured-runnable"
        assert captured["model"] == "anthropic/claude-haiku-5.5"
        assert captured["temperature"] == 0.0
        assert captured["max_tokens"] == max_tokens_for("judge") == 400
        assert captured["disable_thinking"] is True
        assert captured["request_timeout"] == command_safety._TIMEOUT_SECONDS
        captured["base"].with_structured_output.assert_called_once_with(
            SafetyVerdict, method="function_calling", include_raw=True
        )

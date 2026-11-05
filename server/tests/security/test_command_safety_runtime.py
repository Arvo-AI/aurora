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
    SYSTEM_PROMPT,
    SafetyVerdict,
    _fingerprint,
    _verdict_from_payload,
    check_command_safety,
)

_HEX_ALPHABET = set("0123456789abcdef")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        assert "GUARDRAILS_LLM_TIMEOUT_SECONDS" in v.thought
        assert "GUARDRAILS_LLM_MODEL" in v.thought
        assert "admin" in v.thought.lower()


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


class TestTaggedToolPayload:
    """Haiku on Bedrock sometimes puts thought and conclusion inside observation."""

    def test_prompt_asks_for_the_tool_call_only(self):
        assert "Return a JSON object" not in SYSTEM_PROMPT
        assert '{"observation"' not in SYSTEM_PROMPT
        assert "tool call is the only format" in SYSTEM_PROMPT

    def test_both_fields_tagged_inside_observation(self):
        verdict = _verdict_from_payload({
            "observation": (
                'Lists pods in the namespace. '
                'name="thought">Read-only diagnostic.</parameter> '
                'name="conclusion">false'
            ),
        })

        assert verdict is not None
        assert verdict.conclusion is False
        assert verdict.thought == "Read-only diagnostic."
        assert verdict.observation == "Lists pods in the namespace."

    def test_conclusion_tag_keeps_a_real_thought_argument(self):
        verdict = _verdict_from_payload({
            "observation": 'Lists pods. name="conclusion">false',
            "thought": "Read-only.",
        })

        assert verdict is not None
        assert verdict.conclusion is False
        assert verdict.thought == "Read-only."

    def test_parameter_tags_with_a_block(self):
        verdict = _verdict_from_payload({
            "observation": (
                '<parameter name="observation">Compiles and runs a binary.</parameter>'
                '<parameter name="thought">Executing untrusted code.</parameter>'
                '<parameter name="conclusion">true</parameter>'
            ),
        })

        assert verdict is not None
        assert verdict.conclusion is True
        assert verdict.thought == "Executing untrusted code."
        assert verdict.observation == "Compiles and runs a binary."

    def test_missing_fields_without_tags_are_not_a_verdict(self):
        assert _verdict_from_payload({"observation": "missing fields"}) is None

    def test_call_llm_uses_the_tagged_payload(self, monkeypatch):
        raw = MagicMock()
        raw.tool_calls = [{
            "name": "SafetyVerdict",
            "args": {
                "observation": 'Lists pods. name="thought">Read-only.</parameter> name="conclusion">false',
            },
        }]
        raw.invalid_tool_calls = []
        raw.content = ""
        llm = MagicMock()
        llm.invoke.return_value = {"raw": raw, "parsed": None, "parsing_error": ValueError("Field required")}
        monkeypatch.setattr(command_safety, "_create_safety_llm", lambda: llm)
        monkeypatch.setattr(command_safety, "_track_usage", lambda *a, **k: None)

        verdict = command_safety._call_llm("prompt", None, None)

        assert verdict.conclusion is False
        assert verdict.thought == "Read-only."

    def test_call_llm_still_fails_closed_when_the_payload_cannot_be_read(self, monkeypatch):
        raw = MagicMock()
        raw.tool_calls = [{"name": "SafetyVerdict", "args": {"observation": "missing fields"}}]
        raw.invalid_tool_calls = []
        raw.content = ""
        llm = MagicMock()
        llm.invoke.return_value = {
            "raw": raw,
            "parsed": None,
            "parsing_error": ValueError("Field required"),
        }
        monkeypatch.setattr(command_safety, "_create_safety_llm", lambda: llm)
        monkeypatch.setattr(command_safety, "_track_usage", lambda *a, **k: None)

        with pytest.raises(ValueError, match="Field required"):
            command_safety._call_llm("prompt", None, None)


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

    def test_diagnostic_ssh_still_reaches_the_judge(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        safe = SafetyVerdict(
            observation="remote journal", thought="read-only diagnostic", conclusion=False,
        )
        spy = MagicMock(return_value=safe)
        monkeypatch.setattr(command_safety, "check_command_safety", spy)

        result = command_safety.evaluate_command(
            "ssh -i ~/.ssh/id_provider_vm admin@198.51.100.1 'journalctl -u nginx -n 20'",
            tool="terminal_exec",
        )

        assert result.blocked is False
        spy.assert_called_once()

    def test_mutating_ssh_still_reaches_the_judge(self, monkeypatch):
        _patch_config(monkeypatch, enabled=True)
        _silence_audit(monkeypatch)
        bad = SafetyVerdict(
            observation="restart", thought="mutates a service", conclusion=True,
        )
        spy = MagicMock(return_value=bad)
        monkeypatch.setattr(command_safety, "check_command_safety", spy)

        result = command_safety.evaluate_command(
            "ssh admin@198.51.100.1 systemctl restart nginx",
            tool="terminal_exec",
        )

        assert result.blocked is True
        assert result.layer == "llm_judge"
        spy.assert_called_once()

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

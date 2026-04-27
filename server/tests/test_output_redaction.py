"""Tests for L5 output redaction.

Coverage:
- Per-rule positive cases (one canonical leak per shipped rule family)
- Negative cases sourced from placeholders, UUIDs, and example strings
- Idempotence (redact(redact(x)) == redact(x))
- Fail-open behavior when the engine errors
- Integration across the two hooks (send_tool_completion + save_context_history)
  using the audit event stream to prove exactly-one-emission semantics.
"""

from __future__ import annotations

import hashlib
import logging

import pytest

from utils.security import output_redaction
from utils.security.output_redaction import (
    already_redacted,
    redact,
    scan,
)


@pytest.mark.parametrize("label, text, expected_rule", [
    ("aws-access-token",  "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X",          "aws-access-token"),
    ("github-pat",        "token: ghp_q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i", "generic-api-key"),
    ("openai-key",        "OPENAI_API_KEY=sk-proj-R3dA9kLpQ8vN2mT7bX4sFhG1jY5uC6wE0oZ2iP3yM4qK", "generic-api-key"),
    ("slack-bot-token",   "xoxb-1234567890-1234567890123-7K9mP2qR5xN8vB3cL4zJ6tY0", "slack-bot-token"),
    ("jwt",               "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "jwt"),
    ("private-key",       "-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEAr" + "x" * 200 + "\n-----END RSA PRIVATE KEY-----", "private-key"),
    ("k8s-secret-yaml",   "apiVersion: v1\nkind: Secret\nmetadata:\n  name: creds\ndata:\n  password: cGFzc3dvcmRfdmFsdWUxMjM0NQ==", "kubernetes-secret-yaml"),
])
def test_positive_cases_are_redacted(label, text, expected_rule):
    redacted, findings = redact(text)
    assert findings, f"{label}: expected a finding, got none"
    assert any(f.rule_id == expected_rule for f in findings), (
        f"{label}: expected rule {expected_rule}, got {[f.rule_id for f in findings]}"
    )
    assert f"[REDACTED:{expected_rule}]" in redacted
    # Raw value (for the AWS case, the only deterministic one we can assert on)
    # must not appear in the redacted output.
    if label == "aws-access-token":
        assert "AKIA6ODU7H4ZLXKDNQ3X" not in redacted


@pytest.mark.parametrize("label, text", [
    ("aws-example-placeholder", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
    ("env-var-reference",       "aws_access_key_id=${AWS_ACCESS_KEY_ID}"),
    ("plain-uuid",              "trace_id: 7f1e3c85-2b4a-49d6-8c91-a3d4e5f6a7b8"),
    ("base64-benign",           "Content-Length: 1234\nBody: SGVsbG8gV29ybGQ="),
    ("ordinary-prose",          "Running kubectl get pods -n default returned 12 results."),
    ("bash-true",               "result: true"),
    ("empty",                   ""),
])
def test_negative_cases_are_not_redacted(label, text):
    redacted, findings = redact(text)
    assert not findings, f"{label}: expected no findings, got {[f.rule_id for f in findings]}"
    assert redacted == text


def test_redact_is_idempotent():
    text = (
        "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X\n"
        "OPENAI_API_KEY=sk-proj-R3dA9kLpQ8vN2mT7bX4sFhG1jY5uC6wE0oZ2iP3yM4qK\n"
        "trace_id: 7f1e3c85-2b4a-49d6-8c91-a3d4e5f6a7b8"
    )
    once, f1 = redact(text)
    twice, f2 = redact(once)
    assert once == twice, "redact must be idempotent on already-redacted text"
    assert f1, "first pass should produce findings"
    assert not f2, "second pass must produce zero findings"
    assert already_redacted(once)


def test_value_hash_is_stable_and_hex16():
    text = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
    findings = scan(text)
    assert len(findings) == 1
    f = findings[0]
    expected = hashlib.sha256(b"AKIA6ODU7H4ZLXKDNQ3X").hexdigest()[:16]
    assert f.value_hash == expected
    assert len(f.value_hash) == 16
    assert all(c in "0123456789abcdef" for c in f.value_hash)


def test_fail_open_on_engine_exception(monkeypatch, caplog):
    text = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"

    def boom(_):
        raise RuntimeError("synthetic regex engine failure")

    monkeypatch.setattr(output_redaction, "_scan_unsafe", boom)

    with caplog.at_level(logging.WARNING, logger="utils.security.output_redaction"):
        redacted, findings = redact(text)

    assert redacted == text, "fail-open must return original text unchanged"
    assert findings == []
    assert any("redact failed" in rec.message for rec in caplog.records)


def test_overlapping_findings_are_deduplicated():
    text = "token=AKIA6ODU7H4ZLXKDNQ3X"
    _, findings = redact(text)
    # At most one finding per overlapping span; we don't care which rule "wins"
    # as long as the secret is covered once.
    starts = sorted((f.start, f.end) for f in findings)
    for (s1, e1), (s2, e2) in zip(starts, starts[1:]):
        assert e1 <= s2, "findings must be non-overlapping after dedup"


def test_findings_are_ordered_by_position():
    text = (
        "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X "
        "OPENAI_API_KEY=sk-proj-R3dA9kLpQ8vN2mT7bX4sFhG1jY5uC6wE0oZ2iP3yM4qK"
    )
    findings = scan(text)
    assert [f.start for f in findings] == sorted(f.start for f in findings)


class TestHookIntegration:
    """Integration across the three redaction hooks.

    Hook 1 (decorator in ``cloud_tools.py``) is the primary path and feeds
    both the WebSocket notification and the LangGraph ``ToolMessage.content``
    for the next LLM turn. Hooks 2 (``ContextManager._redact_tool_messages``,
    pre-DB) and 3 (``Workflow._redact_for_ui``, UI transcript) are
    belt-and-suspenders passes whose findings-rate is an operational signal
    that an upstream path is bypassing Hook 1.
    """

    def test_later_hooks_are_noop_on_output_already_redacted_by_hook1(self):
        raw = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
        hook1_out, hook1_findings = redact(raw)
        assert hook1_findings and "[REDACTED:" in hook1_out

        hook2_out, hook2_findings = redact(hook1_out)
        assert hook2_out == hook1_out
        assert hook2_findings == []

    def test_later_hooks_catch_output_that_bypassed_hook1(self):
        # Simulates a ToolMessage constructed directly (background chat, some
        # summarization rewrites, etc.) that never flowed through Hook 1.
        raw = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
        out, findings = redact(raw)
        assert findings, "later hooks must redact when Hook 1 was bypassed"
        assert "AKIA6ODU7H4ZLXKDNQ3X" not in out
        assert findings[0].rule_id == "aws-access-token"


def test_size_cap_truncates_scan_but_passes_tail_through():
    from utils.security.output_redaction import MAX_SCAN_BYTES

    filler = "x" * MAX_SCAN_BYTES
    tail = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
    text = filler + tail
    redacted, findings = redact(text)
    assert redacted == text, "content beyond MAX_SCAN_BYTES must pass through unchanged"
    assert findings == [], "scanner must not run on content beyond the cap"


def test_size_cap_redacts_within_head():
    from utils.security.output_redaction import MAX_SCAN_BYTES

    secret = "AKIA6ODU7H4ZLXKDNQ3X"
    head = f"AWS_ACCESS_KEY_ID={secret}\n" + "x" * MAX_SCAN_BYTES
    redacted, findings = redact(head)
    assert findings and findings[0].rule_id == "aws-access-token"
    assert secret not in redacted


def test_fast_path_short_circuits_on_fully_redacted_input(monkeypatch):
    from utils.security import output_redaction as mod

    text = "AWS_ACCESS_KEY_ID=[REDACTED:aws-access-token]\nuser=alice"

    called = {"n": 0}
    original = mod._scan_unsafe

    def counting(s):
        called["n"] += 1
        return original(s)

    monkeypatch.setattr(mod, "_scan_unsafe", counting)

    out, findings = redact(text)
    assert out == text
    assert findings == []
    assert called["n"] == 0, "fast-path must skip _scan_unsafe on fully-redacted input"


def test_fast_path_does_not_short_circuit_when_secret_coexists_with_placeholder():
    text = (
        "AWS_ACCESS_KEY_ID=[REDACTED:aws-access-token]\n"
        "OTHER=AKIA6ODU7H4ZLXKDNQ3X"
    )
    _, findings = redact(text)
    assert findings, "placeholder + real secret must still be scanned"
    assert any(f.rule_id == "aws-access-token" for f in findings)


def test_hook2_context_manager_redacts_tool_messages_and_emits_audit(monkeypatch):
    """Exercises the real Hook 2 code path end-to-end: a ToolMessage with a
    raw secret goes in, a ToolMessage with a placeholder comes out, and the
    audit stream records exactly one emission per finding with the correct
    location marker. Guards against regressions in ToolMessage field
    preservation (model_copy) and audit metadata.
    """
    from langchain_core.messages import ToolMessage

    from chat.backend.agent.utils.persistence import context_manager as cm_mod

    emitted: list[dict] = []

    def fake_emit(**kwargs):
        emitted.append(kwargs)

    monkeypatch.setattr(cm_mod, "_l5_emit", fake_emit)

    instance = cm_mod.ContextManager.__new__(cm_mod.ContextManager)

    raw = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
    msg = ToolMessage(content=raw, tool_call_id="call-123", name="cloud_exec")
    out = instance._redact_tool_messages(
        [msg], user_id="user-42", session_id="sess-7"
    )

    assert len(out) == 1
    assert isinstance(out[0], ToolMessage)
    assert "AKIA6ODU7H4ZLXKDNQ3X" not in out[0].content
    assert "[REDACTED:aws-access-token]" in out[0].content
    assert out[0].tool_call_id == "call-123", "model_copy must preserve tool_call_id"
    assert out[0].name == "cloud_exec", "model_copy must preserve name"

    assert len(emitted) == 1
    assert emitted[0]["location"] == "db_save"
    assert emitted[0]["rule_id"] == "aws-access-token"
    assert emitted[0]["user_id"] == "user-42"
    assert emitted[0]["session_id"] == "sess-7"

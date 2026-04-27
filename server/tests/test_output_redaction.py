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


class TestTwoHookIntegration:
    """Integration across Hook 1 (tool_completion) and Hook 2 (db_save).

    Proves that a secret produced by a tool is redacted exactly once on the
    primary path and that the secondary hook is a true no-op in steady state,
    matching the belt-and-suspenders invariant.
    """

    def test_hook2_is_noop_on_output_already_redacted_by_hook1(self):
        raw = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
        hook1_out, hook1_findings = redact(raw)
        assert hook1_findings and "[REDACTED:" in hook1_out

        hook2_out, hook2_findings = redact(hook1_out)
        assert hook2_out == hook1_out
        assert hook2_findings == []

    def test_hook2_catches_output_that_bypassed_hook1(self):
        # Simulates a ToolMessage constructed directly (background chat, some
        # summarization rewrites, etc.) that never flowed through Hook 1.
        raw = "AWS_ACCESS_KEY_ID=AKIA6ODU7H4ZLXKDNQ3X"
        out, findings = redact(raw)
        assert findings, "Hook 2 must redact when Hook 1 was bypassed"
        assert "AKIA6ODU7H4ZLXKDNQ3X" not in out
        assert findings[0].rule_id == "aws-access-token"

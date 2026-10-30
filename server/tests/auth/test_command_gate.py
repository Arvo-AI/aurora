"""The approval hash has to outlive gate_command. terminal_run reads it
after the gate returns; a hash set inside the try is cleared by the reset.
"""

from utils.auth import command_gate
from utils.auth.command_gate import _policy_reason


def _clear_approval():
    command_gate._guardrails_approved_command.set(None)


def test_allowed_command_stays_approved_after_the_gate_returns(monkeypatch):
    monkeypatch.setattr(command_gate, "_gate_impl", lambda **_kwargs: command_gate._ALLOWED)
    command = "uptime"
    try:
        decision = command_gate.gate_command(
            user_id="uid-admin", tool_name="terminal_exec", command=command,
        )
        assert decision.allowed is True
        assert command_gate.guardrails_approved_hash() == command_gate._hash(command)
    finally:
        _clear_approval()


def test_blocked_command_does_not_approve_the_executor(monkeypatch):
    monkeypatch.setattr(
        command_gate, "_gate_impl",
        lambda **_kwargs: command_gate._block("POLICY_DENIED", "no"),
    )
    try:
        decision = command_gate.gate_command(
            user_id="uid-admin", tool_name="terminal_exec", command="uptime",
        )
        assert decision.allowed is False
        assert command_gate.guardrails_approved_hash() is None
    finally:
        _clear_approval()


def test_executor_skips_the_judge_when_the_gate_approved_the_command(monkeypatch):
    from utils.terminal.terminal_run import _check_guardrails

    calls = []

    def _judge(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("judge should not run")

    monkeypatch.setattr("utils.security.command_safety.evaluate_command", _judge)
    command = "uptime"
    command_gate._guardrails_approved_command.set(command_gate._hash(command))
    try:
        assert _check_guardrails(command) is None
        assert calls == []
    finally:
        _clear_approval()


def test_overlap_sentence_puts_a_period_before_the_allow_clause():
    text = _policy_reason(
        "SSH access (use Standard Operations template to enable)",
        "health-report",
    )
    assert text == (
        "organization policy: SSH access (use Standard Operations template to enable)."
        " It also matches allow rule 'health-report'."
        " The deny rule wins."
    )


def test_overlap_sentence_does_not_double_an_existing_period():
    text = _policy_reason("Blocked.", "health-report")
    assert "Blocked. It also matches" in text
    assert "Blocked.. It also matches" not in text

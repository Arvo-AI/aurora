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


def test_rewritten_argv_is_judged_again_without_a_carried_approval(monkeypatch):
    """cloud_exec adds flags after the gate, so the executor sees another hash."""
    import shlex
    from types import SimpleNamespace
    from utils.terminal.terminal_run import _check_guardrails

    calls = []

    def _judge(*_args, **_kwargs):
        calls.append(1)
        return SimpleNamespace(blocked=False)

    monkeypatch.setattr("utils.security.command_safety.evaluate_command", _judge)
    gated = 'gcloud logging read "severity>=ERROR" --limit 5'
    exec_args = shlex.split(gated + " --project=p1 --format=json")
    command_gate._guardrails_approved_command.set(command_gate._hash(gated))
    try:
        assert _check_guardrails(exec_args) is None
        assert calls == [1]
    finally:
        _clear_approval()


def test_carried_approval_covers_the_argv_the_tool_runs(monkeypatch):
    import shlex
    from utils.terminal.terminal_run import _check_guardrails

    def _judge(*_args, **_kwargs):
        raise AssertionError("judge should not run")

    monkeypatch.setattr("utils.security.command_safety.evaluate_command", _judge)
    gated = 'gcloud logging read "severity>=ERROR" --limit 5'
    exec_args = shlex.split(gated + " --project=p1 --format=json")
    command_gate._guardrails_approved_command.set(command_gate._hash(gated))
    try:
        assert command_gate.carry_guardrails_approval(gated, exec_args) is not None
        assert _check_guardrails(exec_args) is None
    finally:
        _clear_approval()


def test_carried_approval_covers_a_requoted_command_with_no_added_flags(monkeypatch):
    """shlex.split then shlex.join turns double quotes into single quotes."""
    import shlex
    from utils.terminal.terminal_run import _check_guardrails

    def _judge(*_args, **_kwargs):
        raise AssertionError("judge should not run")

    monkeypatch.setattr("utils.security.command_safety.evaluate_command", _judge)
    gated = 'kubectl logs deploy/api --since=1h -n "prod apps"'
    exec_args = shlex.split(gated)
    assert shlex.join(exec_args) != gated
    command_gate._guardrails_approved_command.set(command_gate._hash(gated))
    try:
        assert command_gate.carry_guardrails_approval(gated, exec_args) is not None
        assert _check_guardrails(exec_args) is None
    finally:
        _clear_approval()


def test_approval_is_not_carried_unless_the_gate_approved_that_command():
    for approved in (None, command_gate._hash("kubectl get pods")):
        command_gate._guardrails_approved_command.set(approved)
        try:
            carried = command_gate.carry_guardrails_approval("rm -rf /data", ["rm", "-rf", "/data"])
            assert carried is None
            assert command_gate.guardrails_approved_hash() == approved
        finally:
            _clear_approval()


def test_carried_approval_is_released_back_to_the_gates_own():
    """The tool hands the approval back once the carried command has run."""
    import shlex

    gated = 'gcloud logging read "severity>=ERROR" --limit 5'
    exec_args = shlex.split(gated + " --project=p1 --format=json")
    command_gate._guardrails_approved_command.set(command_gate._hash(gated))
    try:
        token = command_gate.carry_guardrails_approval(gated, exec_args)
        assert command_gate.guardrails_approved_hash() == command_gate._hash(shlex.join(exec_args))
        command_gate.release_guardrails_approval(token)
        assert command_gate.guardrails_approved_hash() == command_gate._hash(gated)
        command_gate.release_guardrails_approval(None)  # nothing was carried: no-op
        assert command_gate.guardrails_approved_hash() == command_gate._hash(gated)
    finally:
        _clear_approval()


# Split quotes hide the token from a check of the string as written; shlex
# (and a shell) put it back together before it runs.
_QUOTE_SPLIT = "kubectl exec web-0 -- sh -c 'r''m -rf /'"


def test_quote_split_example_only_matches_once_it_is_read_as_executed():
    import shlex
    from utils.security.signature_match import check_signature

    assert check_signature(_QUOTE_SPLIT).matched is False
    assert check_signature(shlex.join(shlex.split(_QUOTE_SPLIT))).matched is True


def test_approval_is_not_carried_onto_an_argv_the_signature_matcher_flags(monkeypatch):
    """The judge let the raw string through. The argv that runs still gets the
    executor's full check, exactly as before approvals were carried."""
    import shlex
    from types import SimpleNamespace
    from utils.terminal.terminal_run import _check_guardrails

    exec_args = shlex.split(_QUOTE_SPLIT)
    checked = []

    def _full_check(cmd, **_kwargs):
        checked.append(cmd)
        return SimpleNamespace(blocked=True, reason="matched a signature")

    monkeypatch.setattr("utils.security.command_safety.evaluate_command", _full_check)
    command_gate._guardrails_approved_command.set(command_gate._hash(_QUOTE_SPLIT))
    try:
        assert command_gate.carry_guardrails_approval(_QUOTE_SPLIT, exec_args) is None
        assert command_gate.guardrails_approved_hash() == command_gate._hash(_QUOTE_SPLIT)
        blocked = _check_guardrails(exec_args)
        assert blocked is not None and blocked.returncode == 126
        assert checked == [shlex.join(exec_args)]
    finally:
        _clear_approval()


def test_approval_is_not_carried_when_the_signature_matcher_cannot_be_asked(monkeypatch):
    import shlex

    def _broken(_command):
        raise RuntimeError("rules failed to load")

    monkeypatch.setattr("utils.security.signature_match.check_signature", _broken)
    gated = "kubectl get pods -n prod"
    command_gate._guardrails_approved_command.set(command_gate._hash(gated))
    try:
        assert command_gate.carry_guardrails_approval(gated, shlex.split(gated + " -o json")) is None
    finally:
        _clear_approval()


def _real_gate_with_a_permissive_judge(monkeypatch):
    """Run the real _gate_impl: real signature matcher, a judge that allows
    everything, no org policy, background context."""
    from unittest.mock import MagicMock
    import utils.security.audit_events as audit_mod
    from utils.auth import command_policy, stateless_auth
    from utils.security import command_safety

    monkeypatch.setattr(command_safety, "config", MagicMock(enabled=True))
    monkeypatch.setattr(audit_mod, "emit_block_event", MagicMock())
    judge = MagicMock(return_value=command_safety.SafetyVerdict(
        observation="reads a pod", thought="looks fine", conclusion=False,
    ))
    monkeypatch.setattr(command_safety, "check_command_safety", judge)
    monkeypatch.setattr(stateless_auth, "get_org_id_for_user", lambda _user_id: None)
    monkeypatch.setattr(
        command_policy, "evaluate_compound_command",
        lambda _org_id, _command: command_policy.CommandVerdict(allowed=True),
    )
    monkeypatch.setattr(command_gate, "_get_context", lambda: (False, None))
    return judge


def test_gate_blocks_a_quote_split_command_the_judge_let_through(monkeypatch):
    judge = _real_gate_with_a_permissive_judge(monkeypatch)
    try:
        decision = command_gate.gate_command(
            user_id="uid-admin", tool_name="cloud_exec", command=_QUOTE_SPLIT,
        )
        assert decision.allowed is False
        assert decision.code == "SIGNATURE_MATCHED"
        assert command_gate.guardrails_approved_hash() is None
        # The second look is the signature matcher only: still one judge call.
        assert judge.call_count == 1
    finally:
        _clear_approval()


def test_gate_still_allows_an_ordinary_quoted_command_with_one_judge_call(monkeypatch):
    judge = _real_gate_with_a_permissive_judge(monkeypatch)
    command = 'kubectl logs deploy/api --since=1h -n "prod apps"'
    try:
        decision = command_gate.gate_command(
            user_id="uid-admin", tool_name="cloud_exec", command=command,
        )
        assert decision.allowed is True
        assert command_gate.guardrails_approved_hash() == command_gate._hash(command)
        assert judge.call_count == 1
    finally:
        _clear_approval()


def test_shell_normalized_is_none_when_nothing_changes_or_quoting_is_broken():
    assert command_gate._shell_normalized("kubectl get pods") is None
    assert command_gate._shell_normalized("echo 'unterminated") is None
    assert command_gate._shell_normalized("ba''sh -c id") == "bash -c id"


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

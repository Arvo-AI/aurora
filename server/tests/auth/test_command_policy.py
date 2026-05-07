"""Tests the per-org command policy gate that the agent consults before
running any shell command on user infrastructure. ``evaluate_command``
checks the command against the org's allow/deny rules (with priority
ordering and denylist-wins precedence) and returns a verdict the
command_gate acts on. Pins the verdict shape, the priority/precedence
ordering, and the deliberate fail-open behaviour when the policy DB is
unreachable -- flipping that to fail-closed would silently paralyze
every agent action during a transient DB outage.
"""

import re
from unittest.mock import MagicMock

import pytest

from utils.auth import command_policy
from utils.auth.command_policy import (
    CommandVerdict,
    ListStates,
    PolicyRule,
    derive_pattern_from_command,
    evaluate_command,
    evaluate_compound_command,
    invalidate_cache,
    plan_yes_always,
    validate_pattern,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _rule(
    *,
    rule_id: int,
    mode: str,
    pattern: str,
    description: str = "",
    priority: int = 100,
) -> PolicyRule:
    return PolicyRule(
        id=rule_id,
        mode=mode,
        pattern=pattern,
        description=description,
        priority=priority,
        compiled=re.compile(pattern),
    )


@pytest.fixture(autouse=True)
def reset_cache():
    command_policy._cache.clear()
    yield
    command_policy._cache.clear()


@pytest.fixture()
def policy(monkeypatch):
    """Replace ``_get_cached`` with a deterministic, mutable stub."""
    state = {
        "allow": [],
        "deny": [],
        "states": ListStates(allowlist_enabled=False, denylist_enabled=False),
    }

    def fake_get_cached(_org_id):
        return state["allow"], state["deny"], state["states"]

    monkeypatch.setattr(command_policy, "_get_cached", fake_get_cached)
    return state


# ---------------------------------------------------------------------------
# No org context -- skip the gate
# ---------------------------------------------------------------------------


class TestNoOrgContext:
    """Without an ``org_id`` there is no policy to enforce."""

    @pytest.mark.parametrize("missing", [None, ""])
    def test_falsy_org_id_short_circuits_to_allowed(self, missing, monkeypatch):
        spy = MagicMock()
        monkeypatch.setattr(command_policy, "_get_cached", spy)

        verdict = evaluate_command(missing, "rm -rf /")

        assert verdict.allowed is True
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Both lists disabled -- gate is open by design
# ---------------------------------------------------------------------------


class TestBothListsDisabled:
    def test_returns_allowed_with_disabled_message(self, policy):
        verdict = evaluate_command("org-7", "anything goes")

        assert verdict == CommandVerdict(
            allowed=True,
            rule_description="Policy lists are disabled",
        )

    def test_does_not_consult_rules_when_both_disabled(self, policy):
        policy["deny"] = [_rule(rule_id=1, mode="deny", pattern=r"rm -rf /")]

        assert evaluate_command("org-7", "rm -rf /").allowed is True


# ---------------------------------------------------------------------------
# Denylist behaviour
# ---------------------------------------------------------------------------


class TestDenylist:
    """Deny match -> blocked; carries the matching rule id and description."""

    def test_denylist_match_blocks(self, policy):
        policy["deny"] = [
            _rule(
                rule_id=42,
                mode="deny",
                pattern=r"\brm\s+-rf\s+/",
                description="Recursive root deletion",
            ),
        ]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_command("org-7", "rm -rf /")

        assert verdict.allowed is False
        assert verdict.deny_rule_id == 42
        assert verdict.rule_description == "Recursive root deletion"
        assert verdict.allowlist_exhausted is False

    def test_denylist_no_match_with_allowlist_off_allows(self, policy):
        policy["deny"] = [_rule(rule_id=1, mode="deny", pattern=r"rm -rf /")]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_command("org-7", "ls -la")

        assert verdict.allowed is True
        assert verdict.deny_rule_id is None

    def test_deny_match_records_allowlist_exhausted_when_allowlist_on(self, policy):
        """Yes-Always planner reads ``allowlist_exhausted`` to decide whether
        clicking Always also needs to add an allow rule."""
        policy["deny"] = [_rule(rule_id=1, mode="deny", pattern=r"danger")]
        policy["allow"] = [_rule(rule_id=2, mode="allow", pattern=r"^ls\b")]
        policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=True)

        verdict = evaluate_command("org-7", "danger")

        assert verdict.allowed is False
        assert verdict.deny_rule_id == 1
        assert verdict.allowlist_exhausted is True

    def test_deny_match_with_concurrent_allow_match_clears_exhausted(self, policy):
        """Both deny and allow match -> only the disable-deny mutation needed."""
        policy["deny"] = [_rule(rule_id=1, mode="deny", pattern=r"kubectl exec")]
        policy["allow"] = [_rule(rule_id=2, mode="allow", pattern=r"^kubectl\b")]
        policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=True)

        verdict = evaluate_command("org-7", "kubectl exec my-pod -- ls")

        assert verdict.allowed is False
        assert verdict.deny_rule_id == 1
        assert verdict.allowlist_exhausted is False


# ---------------------------------------------------------------------------
# Allowlist behaviour
# ---------------------------------------------------------------------------


class TestAllowlist:
    """Allowlist on + no match -> blocked. Allowlist on + match -> allowed."""

    def test_allowlist_no_match_blocks_with_exhausted_flag(self, policy):
        policy["allow"] = [_rule(rule_id=1, mode="allow", pattern=r"^ls\b")]
        policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=False)

        verdict = evaluate_command("org-7", "kubectl get pods")

        assert verdict.allowed is False
        assert verdict.allowlist_exhausted is True
        assert verdict.rule_description == "No matching allow rule"
        assert verdict.deny_rule_id is None

    def test_allowlist_match_allows_and_carries_description(self, policy):
        policy["allow"] = [
            _rule(
                rule_id=99,
                mode="allow",
                pattern=r"^kubectl\s+get\b",
                description="Read-only kubectl",
            ),
        ]
        policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=False)

        verdict = evaluate_command("org-7", "kubectl get pods")

        assert verdict.allowed is True
        assert verdict.rule_description == "Read-only kubectl"
        assert verdict.allowlist_exhausted is False

    def test_allowlist_off_does_not_block_when_no_match(self, policy):
        policy["allow"] = [_rule(rule_id=1, mode="allow", pattern=r"^ls\b")]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_command("org-7", "kubectl get pods")

        assert verdict.allowed is True


# ---------------------------------------------------------------------------
# Priority ordering -- first match wins (rules arrive sorted DESC)
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    """``_fetch`` orders rules by priority DESC; ``evaluate_command`` returns
    the first match it sees."""

    def test_higher_priority_deny_rule_wins_over_lower(self, policy):
        policy["deny"] = [
            _rule(rule_id=10, mode="deny", pattern=r"sudo",
                  description="High prio deny", priority=100),
            _rule(rule_id=20, mode="deny", pattern=r"sudo",
                  description="Low prio deny", priority=10),
        ]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_command("org-7", "sudo something")

        assert verdict.deny_rule_id == 10
        assert verdict.rule_description == "High prio deny"

    def test_higher_priority_allow_rule_wins_over_lower(self, policy):
        policy["allow"] = [
            _rule(rule_id=10, mode="allow", pattern=r"^aws\b",
                  description="Specific AWS rule", priority=200),
            _rule(rule_id=20, mode="allow", pattern=r"^aws\b",
                  description="Generic AWS rule", priority=100),
        ]
        policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=False)

        verdict = evaluate_command("org-7", "aws s3 ls")

        assert verdict.allowed is True
        assert verdict.rule_description == "Specific AWS rule"

    def test_first_matching_deny_short_circuits_remaining_rules(self, policy):
        policy["deny"] = [
            _rule(rule_id=1, mode="deny", pattern=r"foo",
                  description="first", priority=100),
            _rule(rule_id=2, mode="deny", pattern=r"foo",
                  description="second-should-not-win", priority=99),
        ]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_command("org-7", "foo bar")

        assert verdict.deny_rule_id == 1
        assert verdict.rule_description == "first"


# ---------------------------------------------------------------------------
# Fail-open: DB outage must not paralyse the agent
# ---------------------------------------------------------------------------


class TestFailOpenOnDbError:
    """``_fetch`` swallows exceptions; a DB outage allows commands by L1.

    Any rewrite that lets an exception out of ``_fetch`` is a silent
    fail-CLOSED regression."""

    def test_fetch_swallows_db_exception_and_returns_disabled_states(
        self, monkeypatch,
    ):
        import utils.db.connection_pool as cp_module

        broken_pool = MagicMock(name="db_pool")
        broken_pool.get_admin_connection.side_effect = RuntimeError(
            "connection refused",
        )
        monkeypatch.setattr(cp_module, "db_pool", broken_pool)

        allow, deny, states = command_policy._fetch("org-7")

        assert allow == []
        assert deny == []
        assert states.allowlist_enabled is False
        assert states.denylist_enabled is False

    def test_evaluate_command_allows_when_fetch_path_fails(self, monkeypatch):
        def failing_fetch(_org_id):
            return [], [], ListStates(
                allowlist_enabled=False, denylist_enabled=False,
            )

        monkeypatch.setattr(command_policy, "_fetch", failing_fetch)

        assert evaluate_command("org-7", "rm -rf /").allowed is True

    def test_partial_db_failure_after_rule_fetch_still_returns_disabled(
        self, monkeypatch,
    ):
        """Rule SELECT succeeds, ``get_user_preference`` raises -> still fail-open."""
        import utils.auth.stateless_auth as sa_module
        import utils.db.connection_pool as cp_module

        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        pool = MagicMock()
        pool.get_admin_connection.return_value.__enter__.return_value = conn
        monkeypatch.setattr(cp_module, "db_pool", pool)
        monkeypatch.setattr(
            sa_module,
            "get_user_preference",
            MagicMock(side_effect=RuntimeError("preference table gone")),
        )

        _, _, states = command_policy._fetch("org-7")

        assert states.allowlist_enabled is False
        assert states.denylist_enabled is False


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    """``apply_yes_always`` calls ``invalidate_cache`` after every mutation."""

    def test_invalidate_cache_drops_only_the_named_org(self):
        command_policy._cache["org-a"] = ([], [], ListStates(False, False), 0.0)
        command_policy._cache["org-b"] = ([], [], ListStates(False, False), 0.0)

        invalidate_cache("org-a")

        assert "org-a" not in command_policy._cache
        assert "org-b" in command_policy._cache

    def test_invalidate_cache_unknown_org_is_noop(self):
        invalidate_cache("never-cached")


# ---------------------------------------------------------------------------
# Compound-command orchestrator
# ---------------------------------------------------------------------------


class TestEvaluateCompoundCommand:
    """``evaluate_compound_command`` splits a shell expression, evaluates each
    atomic part, and blocks the whole thing on the first denial."""

    def test_no_org_id_allows_without_consulting_rules(self, monkeypatch):
        spy = MagicMock()
        monkeypatch.setattr(command_policy, "_get_cached", spy)

        verdict = evaluate_compound_command(None, "rm -rf /")

        assert verdict.allowed is True
        spy.assert_not_called()

    def test_single_allowed_command_passes(self, policy):
        policy["allow"] = [_rule(rule_id=1, mode="allow", pattern=r"^ls\b")]
        policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=False)

        verdict = evaluate_compound_command("org-7", "ls -la")

        assert verdict.allowed is True

    def test_first_denied_subcommand_blocks_entire_expression(self, policy):
        policy["deny"] = [
            _rule(rule_id=1, mode="deny", pattern=r"\brm\s+-rf\s+/"),
        ]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_compound_command("org-7", "ls -la ; rm -rf /")

        assert verdict.allowed is False
        assert verdict.deny_rule_id == 1

    def test_all_safe_subcommands_returns_allowed(self, policy):
        policy["deny"] = [
            _rule(rule_id=1, mode="deny", pattern=r"\brm\s+-rf\s+/"),
        ]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_compound_command("org-7", "ls -la && echo hello")

        assert verdict.allowed is True

    def test_empty_input_falls_back_to_evaluate_command(self, policy):
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=False)

        verdict = evaluate_compound_command("org-7", "   ")

        assert verdict.allowed is True
        assert verdict.rule_description == "Policy lists are disabled"

    def test_pipeline_operator_triggers_per_part_evaluation(self, policy):
        policy["deny"] = [
            _rule(rule_id=5, mode="deny", pattern=r"\bssh-keygen\b"),
        ]
        policy["states"] = ListStates(allowlist_enabled=False, denylist_enabled=True)

        verdict = evaluate_compound_command("org-7", "echo ok | ssh-keygen -t rsa")

        assert verdict.allowed is False
        assert verdict.deny_rule_id == 5


# ---------------------------------------------------------------------------
# Pattern validation and ReDoS protection
# ---------------------------------------------------------------------------


class TestValidatePattern:
    """``validate_pattern`` rejects malformed, overlong, and ReDoS-prone patterns."""

    @pytest.mark.parametrize("pattern", [
        r"^kubectl\b",
        r"\brm\s+-rf\s+/",
        r"^aws\s+\S+\s+describe",
        r"a+b*c?",
    ])
    def test_valid_patterns_return_none(self, pattern):
        assert validate_pattern(pattern) is None

    def test_invalid_regex_returns_error_string(self):
        result = validate_pattern(r"[unclosed")
        assert result is not None
        assert isinstance(result, str)

    def test_too_long_pattern_returns_error(self):
        result = validate_pattern("a" * 501)
        assert result is not None
        assert "too long" in result

    @pytest.mark.parametrize("pattern", [
        r"(a+)+",
        r"(a+)*",
        r"(x*)+",
        r"(a+)?+",
    ])
    def test_nested_quantifiers_rejected(self, pattern):
        result = validate_pattern(pattern)
        assert result is not None
        assert "nested quantifiers" in result

    @pytest.mark.parametrize("pattern", [
        r"a+b+",
        r"(abc)+",
        r"(a|b)+",
    ])
    def test_safe_quantifiers_accepted(self, pattern):
        assert validate_pattern(pattern) is None


# ---------------------------------------------------------------------------
# Pattern derivation from commands
# ---------------------------------------------------------------------------


class TestDerivePattern:
    """``derive_pattern_from_command`` proposes an allow-rule regex from a command."""

    def test_simple_command_anchors_on_cli_plus_subcommands(self):
        result = derive_pattern_from_command("kubectl get pods")
        assert result == r"^kubectl\s+get\s+pods\b"

    def test_strips_leading_sudo(self):
        result = derive_pattern_from_command("sudo kubectl delete pod foo")
        assert result == r"^kubectl\s+delete\s+pod\b"

    def test_strips_sudo_flags(self):
        result = derive_pattern_from_command("sudo -u root aws ec2 terminate")
        assert result == r"^aws\s+ec2\s+terminate\b"

    def test_strips_env_var_prefix(self):
        result = derive_pattern_from_command("KUBECONFIG=/x kubectl get pods")
        assert result == r"^kubectl\s+get\s+pods\b"

    def test_stops_at_flag_arguments(self):
        result = derive_pattern_from_command("aws ec2 terminate-instances --id i-123")
        assert result == r"^aws\s+ec2\s+terminate\-instances\b"

    def test_caps_at_three_tokens(self):
        result = derive_pattern_from_command("gcloud compute instances list --project x")
        assert result == r"^gcloud\s+compute\s+instances\b"

    def test_shell_keyword_falls_back_to_full_escape(self):
        result = derive_pattern_from_command("for f in *.log; do cat $f; done")
        assert result.startswith("^")
        assert result.endswith("$")
        assert r"\s+" not in result

    def test_single_cli_without_subcommand(self):
        result = derive_pattern_from_command("uptime")
        assert result == r"^uptime\b"


# ---------------------------------------------------------------------------
# Yes-Always planner
# ---------------------------------------------------------------------------


class TestPlanYesAlways:
    """``plan_yes_always`` translates a verdict into the exact policy mutations
    the Yes-Always button should apply."""

    def test_deny_hit_only_plans_disable_rule(self):
        verdict = CommandVerdict(
            allowed=False,
            deny_rule_id=42,
            rule_description="Dangerous pattern",
            allowlist_exhausted=False,
        )

        changes = plan_yes_always(verdict, "rm -rf /")

        assert len(changes) == 1
        assert changes[0].action == "disable_deny_rule"
        assert changes[0].rule_id == 42
        assert changes[0].editable is False

    def test_allowlist_exhausted_only_plans_add_allow(self):
        verdict = CommandVerdict(
            allowed=False,
            rule_description="No matching allow rule",
            allowlist_exhausted=True,
        )

        changes = plan_yes_always(verdict, "kubectl get pods")

        assert len(changes) == 1
        assert changes[0].action == "add_allow_rule"
        assert changes[0].pattern is not None
        assert changes[0].editable is True

    def test_deny_hit_with_exhausted_plans_both_changes(self):
        verdict = CommandVerdict(
            allowed=False,
            deny_rule_id=7,
            rule_description="Blocked by deny",
            allowlist_exhausted=True,
        )

        changes = plan_yes_always(verdict, "kubectl exec pod -- ls")

        assert len(changes) == 2
        assert changes[0].action == "disable_deny_rule"
        assert changes[0].rule_id == 7
        assert changes[1].action == "add_allow_rule"
        assert changes[1].editable is True

    def test_allowed_verdict_returns_no_changes(self):
        verdict = CommandVerdict(allowed=True)

        changes = plan_yes_always(verdict, "ls -la")

        assert changes == []

    def test_derived_pattern_is_passed_to_add_allow_change(self):
        verdict = CommandVerdict(
            allowed=False,
            rule_description="No matching allow rule",
            allowlist_exhausted=True,
        )

        changes = plan_yes_always(verdict, "sudo kubectl get pods")

        assert len(changes) == 1
        assert changes[0].pattern == derive_pattern_from_command("sudo kubectl get pods")

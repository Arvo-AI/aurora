"""Regression: the shipped policy templates must let the agent read rollout
history/status while still blocking the rollout subcommands that mutate.

The templates' deny rule used to match the bare ``kubectl rollout`` verb, so
``kubectl rollout history`` (a read) was refused as a "Mutating kubectl
operation" during RCA. The rules here are the real template rules, loaded
through the same ``evaluate_compound_command`` path ``command_gate`` uses, with
both lists enabled as ``seed_default_command_policy`` leaves a new org.
"""

import pytest

from utils.auth import command_policy
from utils.auth.command_policy import (
    ListStates,
    PolicyRule,
    _compile_safe,
    evaluate_compound_command,
    get_policy_templates,
)

READ_ONLY = [
    "kubectl rollout history deployment/worker -n shop",
    "kubectl rollout status deployment/api -n shop",
]

MUTATING = [
    "kubectl rollout restart deployment/api -n shop",
    "kubectl rollout undo deployment/api -n shop",
    "kubectl rollout pause deployment/api -n shop",
    "kubectl rollout resume deployment/api -n shop",
]


def _rules(raw_rules, mode):
    rules = [
        PolicyRule(
            id=i, mode=mode, pattern=r["pattern"], description=r["description"],
            priority=r["priority"], compiled=_compile_safe(r["pattern"]),
        )
        for i, r in enumerate(raw_rules)
    ]
    assert all(r.compiled is not None for r in rules)
    return sorted(rules, key=lambda r: -r.priority)


@pytest.fixture(params=["observability_only", "standard_ops"])
def template_policy(request, monkeypatch):
    tpl = next(t for t in get_policy_templates() if t["id"] == request.param)
    allow, deny = _rules(tpl["allow"], "allow"), _rules(tpl["deny"], "deny")
    states = ListStates(allowlist_enabled=True, denylist_enabled=True)
    command_policy._cache.clear()
    monkeypatch.setattr(command_policy, "_get_cached", lambda _org: (allow, deny, states))
    yield request.param
    command_policy._cache.clear()


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_rollout_reads_are_allowed(template_policy, cmd):
    verdict = evaluate_compound_command("org-1", cmd)
    assert verdict.allowed, (template_policy, cmd, verdict)
    assert not verdict.allowlist_exhausted


@pytest.mark.parametrize("cmd", MUTATING)
def test_rollout_mutations_are_denied_by_deny_rule(template_policy, cmd):
    verdict = evaluate_compound_command("org-1", cmd)
    assert not verdict.allowed, (template_policy, cmd)
    assert verdict.deny_rule_id is not None
    assert verdict.rule_description.startswith("Mutating kubectl operations")

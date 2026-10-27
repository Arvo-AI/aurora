"""Read-only ssh is on the observability allowlist. Writes stay denied.

The template deny matches every `ssh`, so a remote `journalctl` never reached
the allow rules. A custom allow rule names the remote program, not `ssh`.
"""

import re

import pytest

from utils.auth import command_policy
from utils.auth.command_policy import (
    ListStates,
    PolicyRule,
    evaluate_compound_command,
    get_policy_templates,
)

_HOST = "admin@198.51.100.1"


@pytest.fixture
def policy(monkeypatch):
    state = {
        "allow": [],
        "deny": [],
        "states": ListStates(allowlist_enabled=False, denylist_enabled=False),
    }

    def fake_get_cached(_org_id):
        return state["allow"], state["deny"], state["states"]

    monkeypatch.setattr(command_policy, "_get_cached", fake_get_cached)
    return state


def _rules(raw_rules, mode):
    compiled = []
    for i, raw in enumerate(raw_rules):
        compiled.append(PolicyRule(
            id=i + (1000 if mode == "deny" else 0),
            mode=mode,
            pattern=raw["pattern"],
            description=raw["description"],
            priority=raw["priority"],
            compiled=re.compile(raw["pattern"]),
        ))
    return compiled


def _load(policy, template_id, *, extra_allow=None):
    tpl = next(t for t in get_policy_templates() if t["id"] == template_id)
    allow = _rules(tpl["allow"], "allow")
    if extra_allow:
        allow.extend(extra_allow)
    policy["allow"] = allow
    policy["deny"] = _rules(tpl["deny"], "deny")
    policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=True)


def test_observability_allows_read_only_ssh_and_blocks_writes(policy):
    _load(policy, "observability_only")

    allowed = [
        f"ssh {_HOST} uptime",
        f"ssh {_HOST} 'journalctl -u nginx -n 50'",
        f"ssh {_HOST} 'systemctl status nginx'",
        f"ssh {_HOST} 'kubectl get pods'",
        f"ssh {_HOST} 'df -h | grep error'",
        f"ssh {_HOST} 'get system status'",
        f"command -p ssh {_HOST} 'journalctl -u nginx -n 20'",
    ]
    for command in allowed:
        verdict = evaluate_compound_command("org-7", command)
        assert verdict.allowed, (command, verdict.rule_description)

    blocked = [
        f"ssh {_HOST} reboot",
        f"ssh {_HOST} 'systemctl restart nginx'",
        f"ssh {_HOST} 'kubectl delete pod x'",
        f"ssh {_HOST} 'rm -rf /tmp/x'",
        f"scp ./notes.txt {_HOST}:/tmp/notes.txt",
        f"command ssh {_HOST} reboot",
        f"ssh {_HOST} uptime && rm -rf /",
        f"ssh {_HOST}",
        f"ssh {_HOST} 'cat ~/.ssh/id_rsa'",
        f"ssh {_HOST} 'kubectl get secrets'",
        f"ssh {_HOST} 'sort -o /tmp/out /etc/passwd'",
        f"ssh {_HOST} 'date -s 2020-01-01'",
    ]
    for command in blocked:
        verdict = evaluate_compound_command("org-7", command)
        assert not verdict.allowed, command


def test_custom_remote_program_uses_its_allow_rule(policy):
    _load(policy, "observability_only", extra_allow=[
        PolicyRule(
            id=50,
            mode="allow",
            pattern=r"^healthcheck\b",
            description="Custom health check",
            priority=50,
            compiled=re.compile(r"^healthcheck\b"),
        ),
    ])

    ok = evaluate_compound_command("org-7", f"ssh {_HOST} healthcheck")
    assert ok.allowed, ok.rule_description
    assert "Custom health check" in (ok.rule_description or "")

    # `restart` is a write verb, so the custom allow rule cannot cover it.
    blocked = evaluate_compound_command(
        "org-7", f"ssh {_HOST} 'healthcheck restart'",
    )
    assert not blocked.allowed


def test_existing_org_without_the_new_allow_rule_still_permits_reads(policy):
    """Rows seeded before the template change only have the blanket ssh deny."""
    _load(policy, "observability_only")
    policy["allow"] = [
        rule for rule in policy["allow"] if rule.pattern != r"^ssh\s"
    ]

    verdict = evaluate_compound_command("org-7", f"ssh {_HOST} 'get system status'")
    assert verdict.allowed, verdict.rule_description
    assert not evaluate_compound_command("org-7", f"ssh {_HOST} reboot").allowed


def test_standard_ops_still_allows_ssh_writes(policy):
    _load(policy, "standard_ops")
    assert evaluate_compound_command("org-7", f"ssh {_HOST} reboot").allowed


def test_read_only_ssh_rule_does_not_allow_a_write_when_the_denylist_is_off(policy):
    _load(policy, "observability_only")
    policy["states"] = ListStates(allowlist_enabled=True, denylist_enabled=False)
    assert not evaluate_compound_command("org-7", f"ssh {_HOST} reboot").allowed
    assert evaluate_compound_command("org-7", f"ssh {_HOST} uptime").allowed

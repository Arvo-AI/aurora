"""Tests for the form/agent context merge in the scan task.

The merge must be subkey-level: a form entry under a dict key must win for
its own subkeys without dropping the agent's other subkeys under the same
key (form "worker: 700" must not discard the agent's "api: 900").
"""

from routes.github.startup_check_routes import run_startup_check_task


class _Store:
    def __init__(self):
        self.saved = {}

    def __call__(self, scan_id, payload):
        self.saved[scan_id] = payload
        return True


def _run(monkeypatch, form_ctx, agent_ctx):
    store = _Store()
    monkeypatch.setattr(
        "routes.github.startup_check_routes._store_result", store
    )
    # The task imports these lazily, so patching the source modules works.
    monkeypatch.setattr(
        "services.startup_check.context_llm.parse_freeform_facts",
        lambda user_id, facts: (agent_ctx, []),
    )

    captured = {}

    def fake_scan(user_id, repo, ctx):
        captured["ctx"] = ctx
        return {"status": "complete", "verdict": "SAFE", "findings": []}

    monkeypatch.setattr(
        "services.startup_check.scanner.scan_github_repo", fake_scan
    )

    run_startup_check_task("scan-1", "user-1", "o/r", form_ctx, [{"name": "n", "value": "v"}])
    return captured["ctx"]


def test_form_subkey_wins_agent_subkey_survives(monkeypatch):
    """Form worker:700 + agent api:900 -> BOTH present, form value wins on
    a contested subkey."""
    form = {"service_memory_mb": {"worker": 700, "api": 100}}
    agent = {"service_memory_mb": {"api": 900, "web": 500}}

    ctx = _run(monkeypatch, form, agent)

    assert ctx.service_memory_mb == {"worker": 700, "api": 100, "web": 500}


def test_form_list_and_agent_list_union(monkeypatch):
    form = {"known_secrets": ["vault-one"]}
    agent = {"known_secrets": ["external-two", "vault-one"]}

    ctx = _run(monkeypatch, form, agent)

    assert sorted(ctx.known_secrets) == ["external-two", "vault-one"]


def test_agent_only_keys_pass_through(monkeypatch):
    form = {"cluster_domain": "cluster.local"}
    agent = {"storage_classes": ["gp2"], "cluster_domain": "wrong.example"}

    ctx = _run(monkeypatch, form, agent)

    assert ctx.cluster_domain == "cluster.local"  # form wins outright
    assert ctx.storage_classes == ["gp2"]  # agent-only key survives

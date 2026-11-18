"""Aurora Watch incidents get a full RCA: RCA mode, an untruncated payload, a useful quick summary."""

from services.watch import report
from services.watch.detector import group_findings

from .k8s import healthy_logs, shop
from .test_detector import learned, run


def _payload():
    c = shop()
    state = learned(c)
    c.set_selector("frontend", {"app": "frontend", "tier": "web"})
    logs = healthy_logs()
    logs["Deployment/shop/loadgen"] = ["GET /api/items -> timeout"] * 3
    group = next(g for g in group_findings(run(c, state, 3, logs)) if g["opens_incident"])
    return group, report.rca_payload(group, {"cluster_id": "kc-1", "name": "prod-east"})


def test_aurora_watch_runs_in_rca_mode():
    import chat.background.task as task_mod

    assert "aurora_watch" in task_mod._RCA_SOURCES


def test_the_rca_prompt_carries_findings_and_suspects_untruncated():
    from chat.background.rca_prompt_builder import build_rca_prompt

    group, payload = _payload()
    prompt, _ = build_rca_prompt("aurora_watch", payload["title"], payload, user_id=None)
    assert "# ROOT CAUSE ANALYSIS REQUIRED - AURORA_WATCH ALERT" in prompt
    assert "selector {app: frontend} -> {app: frontend, tier: web}" in prompt
    assert "[field truncated]" not in prompt
    # Every evidence line reaches the agent whole.
    for finding in payload["findings"]:
        for line in finding["evidence"]:
            assert line in prompt.replace('\\"', '"')


def test_the_quick_summary_names_what_regressed_and_the_suspect():
    from chat.background.summarization import _build_summary_prompt

    _, payload = _payload()
    prompt = _build_summary_prompt("aurora_watch", payload["title"], "critical", "frontend",
                                   payload, {"aurora_watch": payload})
    assert "What regressed:" in prompt
    assert "Suspected change: Service/shop/frontend" in prompt

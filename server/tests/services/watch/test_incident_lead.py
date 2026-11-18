"""A new incident leads with the change it is keyed on, also when its group holds a
downstream finding (listed first, as kubectl lists Deployments by name) whose own
primary suspect is another change."""
import pytest

from services.watch import report
from services.watch.detector import DEFAULTS, group_findings, new_state, step
from services.watch.snapshot import build_snapshot

from .k8s import Cluster, healthy_logs


def _cluster() -> Cluster:
    c = Cluster()
    c.configmap("api-code", {"api.py": "print('api')", "worker.py": "print('worker')"})
    c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8080; }"})
    c.deployment("api", replicas=2, env={"REDIS_HOST": "redis"}, configmap="api-code")
    c.deployment("frontend", replicas=2, configmap="frontend-nginx")
    c.deployment("loadgen")
    c.deployment("redis")
    c.deployment("worker", env={"BATCH_SIZE": "500"}, configmap="api-code")
    c.service("api", {"app": "api"}, 8080, 8080)
    c.service("frontend", {"app": "frontend"}, 80, 80)
    c.service("redis", {"app": "redis"}, 6379, 6379)
    return c


def _poll(c, state, logs=None):
    _, confirmed = step(state, build_snapshot(c.render(), c.now), healthy_logs() if logs is None else logs)
    c.tick(60)
    return confirmed


@pytest.mark.parametrize("before", ["deploy_10_min_before", "same_release", "restart_after"])
def test_the_incident_leads_with_the_change_it_is_keyed_on(before):
    c = _cluster()
    state = new_state()
    for _ in range(DEFAULTS["learning_polls"] + 3):
        _poll(c, state)
    if before == "deploy_10_min_before":
        c.set_env("api", "FEATURE_FLAGS", "new-checkout")  # harmless
        for _ in range(10):
            _poll(c, state)
    elif before == "same_release":
        c.set_env("api", "FEATURE_FLAGS", "new-checkout")  # harmless, shipped with redis's
    c.set_env("redis", "MAXMEMORY", "1mb")  # the bad change: redis crash-loops
    c.crash("redis", reason="Error", exit_code=1)
    logs = healthy_logs()
    logs["Deployment/shop/api"] = ['10.244.0.7 "GET /items HTTP/1.0" 503 -'] * 3
    if before == "restart_after":
        _poll(c, state, logs)
        c.restart("api")  # the first remediation, before Watch confirms
    for _ in range(3):
        _poll(c, state, logs)
    unreported = [f for f in state["open"].values() if f["severity"] in ("high", "critical")]
    (group,) = group_findings(unreported)
    assert group["key"].split("|")[1] == "Deployment/shop/redis"
    payload = report.rca_payload(group, {"cluster_id": "c1", "name": "prod"})
    assert payload["suspected_changes"][0]["object"] == "Deployment/shop/redis"
    assert "Suspected change: Deployment/shop/redis" in payload["message"]

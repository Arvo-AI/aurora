"""Snapshot model and change tracking: what is kept, what is never kept, and how changes read."""

import json

from services.watch.changes import detect_changes
from services.watch.snapshot import build_snapshot

from .k8s import shop


def test_pods_map_to_their_deployment_through_the_replicaset():
    c = shop()
    snap = build_snapshot(c.render(), c.now)
    workloads = {p["workload"] for p in snap["pods"].values()}
    assert workloads == {f"Deployment/shop/{n}" for n in ("redis", "api", "worker", "frontend", "loadgen")}
    assert snap["services"]["shop/api"]["backends"] == ["Deployment/shop/api"]
    assert snap["services"]["shop/api"]["ready_endpoints"] == 2


def test_credentials_in_env_are_redacted_and_configmap_values_are_never_kept():
    c = shop()
    c.set_env("api", "DB_PASSWORD", "hunter2")
    c.configmap("api-code", {"api.py": "TOKEN = 'sk-live-123'"})
    snap = build_snapshot(c.render(), c.now)
    blob = json.dumps(snap)
    assert "hunter2" not in blob and "sk-live-123" not in blob
    env = snap["workloads"]["Deployment/shop/api"]["template"]["containers"]["api"]["env"]
    assert env["DB_PASSWORD"] == "<redacted>" and env["REDIS_HOST"] == "redis"


def test_credentials_inside_innocently_named_values_and_args_are_redacted():
    c = shop()
    c.set_env("api", "DATABASE_URL", "postgres://shop:s3cr3t-pa55@db.internal:5432/shop")
    container = c.deployments["api"]["template"]["spec"]["containers"][0]
    container["args"] = ["--github-token", "ghp_q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i"]
    blob = json.dumps(build_snapshot(c.render(), c.now))
    assert "s3cr3t-pa55" not in blob and "postgres://shop:<redacted>@db.internal" in blob
    assert "ghp_q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i" not in blob


def test_probe_headers_and_exec_arguments_are_redacted():
    c = shop()
    container = c.deployments["api"]["template"]["spec"]["containers"][0]
    container["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": 8080,
                                              "httpHeaders": [{"name": "Authorization", "value": "Bearer abc.def.ghi"}]}}
    container["readinessProbe"] = {"exec": {"command": ["redis-cli", "-a", "postgres://u:hunter2hunter2@x/y", "ping"]}}
    snap = build_snapshot(c.render(), c.now)
    blob = json.dumps(snap)
    assert "Bearer abc.def.ghi" not in blob and "hunter2hunter2" not in blob
    probe = snap["workloads"]["Deployment/shop/api"]["template"]["containers"]["api"]["livenessProbe"]
    assert probe["httpGet"]["port"] == 8080  # what a change diff needs survives


def test_secret_references_are_kept_as_references():
    c = shop()
    c.deployments["api"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "api-secrets", "key": "db-password"}}})
    snap = build_snapshot(c.render(), c.now)
    w = snap["workloads"]["Deployment/shop/api"]
    assert w["template"]["containers"]["api"]["env"]["DB_PASSWORD"] == "<from secret api-secrets/db-password>"
    assert w["references"]["Secret"] == ["api-secrets"]
    assert w["references"]["ConfigMap"] == ["api-code"]


def test_changes_read_like_a_diff():
    c = shop()
    before = build_snapshot(c.render(), c.now)
    c.tick()
    c.set_env("worker", "BATCH_SIZE", "2500")
    c.set_selector("frontend", {"app": "frontend", "tier": "web"})
    c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8000; }"})
    after = build_snapshot(c.render(), c.now)
    changes = {ch["key"]: ch for ch in detect_changes(before, after)}
    assert changes["Deployment/shop/worker"]["details"] == ["container worker: env BATCH_SIZE 500 -> 2500"]
    assert changes["Service/shop/frontend"]["details"] == ["selector {app: frontend} -> {app: frontend, tier: web}"]
    assert changes["ConfigMap/shop/frontend-nginx"]["details"] == ["ConfigMap key default.conf changed"]
    assert "upstream" not in json.dumps(changes["ConfigMap/shop/frontend-nginx"])  # contents never leave the cluster
    for ch in changes.values():
        assert before["taken_at"] <= ch["at"] <= after["taken_at"]


def test_no_changes_between_identical_snapshots():
    c = shop()
    a = build_snapshot(c.render(), c.now)
    c.tick()
    assert detect_changes(a, build_snapshot(c.render(), c.now)) == []

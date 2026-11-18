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
    fake_pat = "ghp_" + "q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i"  # synthetic; split so none is committed whole
    container["args"] = ["--github-token", fake_pat]
    blob = json.dumps(build_snapshot(c.render(), c.now))
    assert "s3cr3t-pa55" not in blob and "postgres://shop:<redacted>@db.internal" in blob
    assert fake_pat not in blob


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


def test_rollout_strategy_follows_kubernetes_defaults_and_rounding():
    from services.watch.snapshot import rollout_strategy

    # Deployment: maxUnavailable 25% rounds down, maxSurge 25% rounds up.
    assert rollout_strategy("Deployment", {}, 4, 4) == (False, 1)
    assert rollout_strategy("Deployment", {}, 2, 2) == (False, 0)
    # Both rounding down to zero: the controller takes one pod down at a time.
    rolling = {"strategy": {"rollingUpdate": {"maxSurge": 0, "maxUnavailable": "10%"}}}
    assert rollout_strategy("Deployment", rolling, 4, 4) == (False, 1)
    assert rollout_strategy("Deployment", {"strategy": {"type": "Recreate"}}, 3, 3) == (False, 3)
    assert rollout_strategy("Deployment", {"paused": True}, 2, 1) == (True, 0)
    # StatefulSet: OnDelete waits for someone to delete pods; a partition keeps the rest old.
    assert rollout_strategy("StatefulSet", {"updateStrategy": {"type": "OnDelete"}}, 3, 0)[0] is True
    partitioned = {"updateStrategy": {"rollingUpdate": {"partition": 2}}}
    assert rollout_strategy("StatefulSet", partitioned, 3, 0)[0] is False  # still rolling ordinal 2
    assert rollout_strategy("StatefulSet", partitioned, 3, 1)[0] is True
    # DaemonSet: maxUnavailable as a percentage rounds up.
    assert rollout_strategy("DaemonSet", {"updateStrategy": {"rollingUpdate": {"maxUnavailable": "10%"}}}, 12, 12) == (False, 2)
    assert rollout_strategy("DaemonSet", {}, 5, 5) == (False, 1)


def test_password_only_urls_flag_values_and_probe_tokens_are_redacted():
    from services.watch.snapshot import _argv, _env_value, _probe

    pw = "Zq8vN3xL2pR7tK9wM4bY"
    assert _env_value({"name": "REDIS_URL", "value": f"redis://:{pw}@redis:6379/0"}) == "redis://:<redacted>@redis:6379/0"
    assert pw not in _env_value({"name": "DB_URL", "value": f"postgres://app:Zq8v/{pw}@db:5432/app"})
    assert _argv(["redis-server", "--requirepass", pw]) == ["redis-server", "--requirepass", "<redacted>"]
    assert _argv(["sh", "-c", f"redis-server --requirepass {pw} --port 6379"]) == [
        "sh", "-c", "redis-server --requirepass <redacted> --port 6379"]
    # A flag after a credential-named one is not its value.
    assert _argv(["--token=abc123", "--disable-auth", "--port", "80"]) == ["--token=<redacted>", "--disable-auth", "--port", "80"]
    assert pw not in json.dumps(_probe({"httpGet": {"path": f"/-/readiness?token={pw}", "port": 8080}}))
    assert _env_value({"name": "API_BASE", "value": "https://api.example.com/v1/items"}) == "https://api.example.com/v1/items"


def test_messages_the_cluster_writes_are_redacted_before_they_are_kept():
    c = shop()
    probe_value = "Hd4sW9qT2m" + "K7yB3nR8vX"
    c.warning("Pod", c.pod_name("api", 0), "Unhealthy",
              f'Readiness probe failed: Get "http://10.244.0.7:8080/-/readiness?token={probe_value}": context deadline exceeded')
    snap = build_snapshot(c.render(), c.now)
    assert probe_value not in json.dumps(snap) and "Readiness probe failed" in json.dumps(snap)


def test_a_configmap_with_thousands_of_keys_makes_a_bounded_change_record():
    c = shop()
    prev = build_snapshot(c.render(), c.now)
    c.configmap("dashboards", {f"board-{i}.json": "{}" for i in range(5000)})
    cur = build_snapshot(c.render(), c.now + 60)
    [created] = [ch for ch in detect_changes(prev, cur) if ch["name"] == "dashboards"]
    assert "and 4990 more" in created["details"][0] and len(json.dumps(created)) < 1000
    c.configmap("dashboards", {f"board-{i}.json": "[]" for i in range(5000)})
    later = build_snapshot(c.render(), c.now + 120)
    [changed] = [ch for ch in detect_changes(cur, later) if ch["name"] == "dashboards"]
    assert len(changed["details"]) <= 6 and len(json.dumps(changed)) < 1000

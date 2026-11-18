"""Snapshot model and change tracking: what is kept, what is never kept, and how changes read."""

import json

import pytest

from services.watch import snapshot as snapshot_mod
from services.watch.changes import detect_changes
from services.watch.snapshot import build_snapshot

from .k8s import shop


@pytest.fixture
def digest_key(monkeypatch):
    """A server secret to key masked-value digests with (INTERNAL_API_SECRET in Aurora)."""
    monkeypatch.setattr(snapshot_mod, "_DIGEST_KEY", b"test-internal-api-secret")
    snapshot_mod._clean.cache_clear()
    yield
    snapshot_mod._clean.cache_clear()


def test_pods_map_to_their_deployment_through_the_replicaset():
    c = shop()
    snap = build_snapshot(c.render(), c.now)
    workloads = {p["workload"] for p in snap["pods"].values()}
    assert workloads == {f"Deployment/shop/{n}" for n in ("redis", "api", "worker", "frontend", "loadgen")}
    assert snap["services"]["shop/api"]["backends"] == ["Deployment/shop/api"]
    assert snap["services"]["shop/api"]["ready_endpoints"] == 2


def test_credentials_in_env_are_redacted_and_configmap_values_are_never_kept(digest_key):
    c = shop()
    c.set_env("api", "DB_PASSWORD", "hunter2")
    c.configmap("api-code", {"api.py": "TOKEN = 'sk-live-123'"})
    snap = build_snapshot(c.render(), c.now)
    blob = json.dumps(snap)
    assert "hunter2" not in blob and "sk-live-123" not in blob
    env = snap["workloads"]["Deployment/shop/api"]["template"]["containers"]["api"]["env"]
    assert env["DB_PASSWORD"].startswith("<redacted #") and env["REDIS_HOST"] == "redis"


def test_a_rotated_credential_is_still_a_change_without_its_value_being_kept(digest_key):
    # The outage after a password rotation must still blame the rotation.
    c = shop()
    c.set_env("api", "DB_PASSWORD", "old-pw-2025")
    prev = build_snapshot(c.render(), c.now)
    c.set_env("api", "DB_PASSWORD", "new-pw-2026")
    cur = build_snapshot(c.render(), c.now + 60)
    [change] = [ch for ch in detect_changes(prev, cur) if ch["key"] == "Deployment/shop/api"]
    detail = change["details"][0]
    assert "env DB_PASSWORD <redacted #" in detail and "-> <redacted #" in detail
    assert "old-pw-2025" not in json.dumps(change) and "new-pw-2026" not in json.dumps(cur)


def test_credentials_inside_innocently_named_values_and_args_are_redacted(digest_key):
    c = shop()
    c.set_env("api", "DATABASE_URL", "postgres://shop:s3cr3t-pa55@db.internal:5432/shop")
    container = c.deployments["api"]["template"]["spec"]["containers"][0]
    fake_pat = "ghp_" + "q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i"  # synthetic; split so none is committed whole
    container["args"] = ["--github-token", fake_pat]
    blob = json.dumps(build_snapshot(c.render(), c.now))
    assert "s3cr3t-pa55" not in blob and "postgres://shop:<redacted #" in blob
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


def test_password_only_urls_flag_values_and_probe_tokens_are_redacted(digest_key):
    from services.watch.snapshot import _argv, _env_value, _probe

    pw = "Zq8vN3xL2pR7tK9wM4bY"
    url = _env_value({"name": "REDIS_URL", "value": f"redis://:{pw}@redis:6379/0"})
    assert url.startswith("redis://:<redacted #") and url.endswith(">@redis:6379/0")
    assert pw not in _env_value({"name": "DB_URL", "value": f"postgres://app:Zq8v/{pw}@db:5432/app"})
    args = _argv(["redis-server", "--requirepass", pw])
    assert args[:2] == ["redis-server", "--requirepass"] and args[2].startswith("<redacted #")
    joined = _argv(["sh", "-c", f"redis-server --requirepass {pw} --port 6379"])[2]
    assert pw not in joined and joined.startswith("redis-server --requirepass <redacted #") and joined.endswith("--port 6379")
    # A flag after a credential-named one is not its value; a flag that only mentions a
    # credential word (--token-ttl, --keyspace) is not a credential.
    flags = _argv(["--token=abc123", "--disable-auth", "--port", "80", "--token-ttl", "3600", "--keyspace", "orders"])
    assert flags[0].startswith("--token=<redacted #") and flags[1:] == [
        "--disable-auth", "--port", "80", "--token-ttl", "3600", "--keyspace", "orders"]
    # Query credentials in a probe path, short or long, and a probe's own exec flags.
    probe = _probe({"httpGet": {"path": "/-/ready?token=hunter2&verbose=1", "port": 8080},
                    "exec": {"command": ["redis-cli", "--pass", "hunter2-prod-pw", "ping"]}})
    assert "hunter2" not in json.dumps(probe) and "&verbose=1" in probe["httpGet"]["path"]
    assert probe["exec"]["command"][3] == "ping"
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


def test_without_a_server_secret_masked_values_carry_no_digest(monkeypatch):
    # A constant key in the source would make every digest checkable against guesses.
    monkeypatch.setattr(snapshot_mod, "_DIGEST_KEY", None)
    snapshot_mod._clean.cache_clear()
    try:
        assert snapshot_mod._env_value({"name": "DB_PASSWORD", "value": "hunter2"}) == "<redacted>"
        assert snapshot_mod._argv(["--requirepass", "hunter2"]) == ["--requirepass", "<redacted>"]
    finally:
        snapshot_mod._clean.cache_clear()


def test_credential_flags_that_end_in_key_or_token_id_are_masked(digest_key):
    args = snapshot_mod._argv(["--account-key", "Zk3pQ9vX2mL7tR4wB8nC", "--master-key", "m",
                               "-dev-root-token-id", "root", "--passphrase", "p", "--max-keys", "1000",
                               "--password-file", "/etc/pw", "--web.key-file", "/certs/k.pem"])
    assert [a.startswith("<redacted") for a in args[1::2]] == [True, True, True, True, False, False, False]


def test_a_long_value_with_a_hidden_part_still_shows_an_edit(digest_key):
    # Values are cut at 120 characters; a token rotated past the cut is still a change.
    def env(token):
        return snapshot_mod._env_value({"name": "APP_CONFIG", "value": "x" * 130 + token})

    assert env("aaaa") != env("bbbb") and "aaaa" not in env("aaaa")


def test_query_parameters_are_masked_in_linear_time():
    import time

    start = time.perf_counter()
    for i in range(400):  # crafted env values at the length cap, a fresh one each time
        snapshot_mod._clean("0001" + "?key" * 249 + str(i))
    assert time.perf_counter() - start < 2
    assert "hunter2" not in snapshot_mod._clean("/-/ready?token=hunter2&verbose=1")
    assert snapshot_mod._clean("/items?page=2&sort=name") == "/items?page=2&sort=name"


def test_a_credential_after_a_nested_query_string_is_masked():
    for text in ("/login?next=/orders?password=Winter2026", "/ready?x=1?sig=Winter2026",
                 "/probe?module=http_2xx&target=http://api:8080/health?token=Winter2026"):
        assert "Winter2026" not in snapshot_mod._clean(text, digest=False), text
    # A credential that holds a "?" is masked whole, and other parameters are kept.
    masked = snapshot_mod._clean("/-/ready?password=Ab?12345&verbose=1", digest=False)
    assert "12345" not in masked and masked.endswith("&verbose=1")
    assert snapshot_mod._clean("/search?q=what?&lang=en", digest=False) == "/search?q=what?&lang=en"

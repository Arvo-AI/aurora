"""Aurora Watch detector: regressions against a learned baseline, and who caused them.

Every test drives the real pipeline (build_snapshot -> detect_changes -> step)
over a small cluster that is broken the way Aurora Lab breaks its ``shop`` app.
"""

from __future__ import annotations

import json

from services.watch.detector import DEFAULTS, group_findings, new_state, step, template_counts
from services.watch.snapshot import build_snapshot

from .k8s import Cluster, healthy_logs, shop

LEARN = DEFAULTS["learning_polls"]


def poll(cluster: Cluster, state: dict, logs: dict | None = None, *, endpoint_slices: bool = True,
         seconds: float = 10.0) -> list[dict]:
    snapshot = build_snapshot(cluster.render(endpoint_slices=endpoint_slices), cluster.now)
    _, confirmed = step(state, snapshot, healthy_logs() if logs is None else logs)
    cluster.tick(seconds)
    return confirmed


def learned(cluster: Cluster, **kwargs) -> dict:
    state = new_state()
    for _ in range(LEARN):
        assert poll(cluster, state, **kwargs) == []
    return state


def run(cluster: Cluster, state: dict, polls: int, logs: dict | None = None, **kwargs) -> list[dict]:
    found = []
    for _ in range(polls):
        found += poll(cluster, state, logs, **kwargs)
    return found


def by_subject(findings: list[dict]) -> dict[str, dict]:
    return {f["subject"]: f for f in findings}


class TestQuietWhenNothingRegresses:
    def test_learning_never_reports_even_on_a_broken_cluster(self):
        c = shop()
        c.crash("worker")
        state = new_state()
        for _ in range(LEARN):
            assert poll(c, state) == []

    def test_a_healthy_cluster_stays_quiet(self):
        c = shop()
        state = learned(c)
        assert run(c, state, 30) == []

    def test_a_rolling_update_that_progresses_is_quiet(self):
        # maxUnavailable lets ready replicas dip while pods are replaced: expected.
        c = shop()
        c.scale("api", 4)
        c.deployments["api"]["strategy"] = {"type": "RollingUpdate",
                                            "rollingUpdate": {"maxUnavailable": "50%", "maxSurge": "25%"}}
        state = learned(c)
        c.roll("api", unready=2)
        assert run(c, state, 8) == []

    def test_a_rollout_losing_more_pods_than_its_strategy_allows_is_reported(self):
        # Kubernetes' default (25% of 4 = 1 pod) never takes 3 down: something else did.
        c = shop()
        c.scale("api", 4)
        c.deployments["api"]["strategy"] = {"type": "RollingUpdate"}
        state = learned(c)
        c.stall("api", updated=1)
        c.scale("api", 4, unready=3)
        found = by_subject(run(c, state, 3))
        assert found["Deployment/shop/api"]["symptoms"][0]["kind"] == "unavailable"

    def test_paused_and_on_delete_rollouts_are_not_stuck(self):
        c = shop()
        state = learned(c)
        c.stall("api", updated=1)  # 1 of 2 on the new revision, then paused on purpose
        c.pause("api")
        assert run(c, state, DEFAULTS["rollout_stuck_seconds"] // 10 + 4) == []

    def test_a_held_rollout_replacing_a_pod_keeps_its_allowance(self):
        # Held means "not stuck", not "not a rollout": a pod of a paused (or OnDelete)
        # rollout still starting, within maxUnavailable, is not an outage.
        c = shop()
        state = learned(c)
        c.stall("api", updated=1)
        c.pause("api")
        c.scale("api", 2, unready=1)
        assert run(c, state, 6) == []

    def test_a_broken_at_start_baseline_without_a_floor_keeps_the_old_rule(self):
        # No floor recorded: quiet until healthy once, then breaking again is reported.
        c = shop()
        c.crash("worker")
        state = learned(c)
        baseline = state["baseline"]["workloads"]["Deployment/shop/worker"]
        del baseline["min_ready"], baseline["max_failing_pods"]  # as a baseline from before they existed
        assert run(c, state, 8) == []
        c.heal("worker")
        assert run(c, state, 3) == []
        c.crash("worker")
        assert "Deployment/shop/worker" in by_subject(run(c, state, 3))

    def test_the_last_pod_of_a_rollout_starting_slowly_is_quiet(self):
        # Everything is "updated" once the last pod is scheduled; it may take minutes to be ready.
        c = shop()
        c.scale("api", 3)
        state = learned(c)
        c.roll("api", unready=1, settle_ticks=12)  # 2 minutes at 10s polls
        assert run(c, state, 20) == []

    def test_scaling_to_zero_is_not_an_outage(self):
        c = shop()
        state = learned(c)
        c.scale("api", 0)
        assert run(c, state, 4) == []

    def test_a_scale_up_waiting_for_new_pods_is_not_lost_capacity(self):
        c = shop()
        state = learned(c)
        c.scale("api", 4, unready=2)  # two new pods still starting
        assert run(c, state, 4) == []

    def test_a_one_poll_blip_of_errors_does_not_open_an_incident(self):
        c = shop()
        state = learned(c)
        blip = healthy_logs()
        blip["Deployment/shop/loadgen"] = ["GET /api/items -> 502"] * 3 + ["GET /api/items -> 200"]
        found = poll(c, state, blip) + run(c, state, 6)
        assert found == []

    def test_a_workload_that_was_already_broken_is_not_reported_until_it_recovers(self):
        # The baseline is the cluster as found: a pod crash-looping before Watch started
        # is not a regression, but breaking again after it recovered is.
        c = shop()
        c.crash("worker")
        state = learned(c)
        assert run(c, state, 8) == []
        c.heal("worker")
        assert run(c, state, 3) == []
        c.crash("worker")
        found = run(c, state, 3)
        assert [f["subject"] for f in found] == ["Deployment/shop/worker"]
        assert any(g["opens_incident"] for g in group_findings(found))

    def test_a_workload_partly_broken_at_start_is_reported_when_it_gets_worse(self):
        # One of two replicas never ready while learning; later both crash-loop.
        c = shop()
        c.scale("worker", 2, unready=1)
        state = learned(c)
        assert state["baseline"]["workloads"]["Deployment/shop/worker"]["broken_at_start"]
        assert run(c, state, 5) == []  # no worse than it was: quiet
        c.set_env("worker", "BATCH_SIZE", "2500")
        c.crash("worker")
        found = by_subject(run(c, state, 4))
        worker = found["Deployment/shop/worker"]
        assert worker["severity"] == "critical" and "0/2 replicas ready (it had 1)" in worker["summary"]
        assert worker["suspects"][0]["details"] == ["container worker: env BATCH_SIZE 500 -> 2500"]


class TestObjectsCreatedLater:
    def test_a_service_and_workload_deployed_after_learning_are_covered_once_learned(self):
        c = shop()
        state = learned(c)
        c.deployment("checkout", replicas=2)
        c.service("checkout", {"app": "checkout"})
        assert run(c, state, LEARN + 2) == []  # its own learning window, quietly
        c.crash("checkout", reason="Error", exit_code=1)
        found = by_subject(run(c, state, 3))
        assert found["Deployment/shop/checkout"]["severity"] == "critical"
        assert found["Service/shop/checkout"]["severity"] == "critical"

    def test_a_new_deployment_that_logs_request_failures_is_reported(self):
        # Aurora Lab scenario 15: a stray Deployment joins a Service and answers 500s.
        c = shop()
        state = learned(c)
        c.deployment("shop-api")
        logs = healthy_logs()
        logs["Deployment/shop/shop-api"] = ['10.0.0.9 "GET /items HTTP/1.0" 500 -'] * 3
        found = by_subject(run(c, state, LEARN + 3, logs))
        assert found["Deployment/shop/shop-api"]["severity"] == "high"


class TestServiceRegression:
    def test_a_selector_that_matches_nothing_is_critical_and_blames_the_service(self):
        c = shop()
        state = learned(c)
        c.set_selector("frontend", {"app": "frontend", "tier": "web"})
        assert poll(c, state) == []  # one poll is not a finding
        found = by_subject(poll(c, state))
        f = found["Service/shop/frontend"]
        assert f["severity"] == "critical"
        assert "no ready endpoints" in f["summary"]
        top = f["suspects"][0]
        assert top["key"] == "Service/shop/frontend"
        assert top["details"] == ["selector {app: frontend} -> {app: frontend, tier: web}"]

    def test_v1_endpoints_work_when_endpointslices_are_not_readable(self):
        # The kubectl-agent's default RBAC grants Endpoints but not EndpointSlices.
        c = shop()
        state = learned(c, endpoint_slices=False)
        c.set_selector("api", {"app": "api-v2"})
        found = by_subject(run(c, state, 2, endpoint_slices=False))
        assert found["Service/shop/api"]["severity"] == "critical"


class TestWorkloadRegression:
    def test_an_oom_crashloop_is_reported_with_its_exit_reason_and_cause(self):
        c = shop()
        state = learned(c)
        c.set_env("worker", "BATCH_SIZE", "2500")
        c.crash("worker", reason="OOMKilled", exit_code=137)
        found = by_subject(run(c, state, 3))
        f = found["Deployment/shop/worker"]
        assert f["severity"] == "critical"  # 0 of 1 ready
        assert any("OOMKilled, code 137" in e for e in f["evidence"])
        assert f["suspects"][0]["key"] == "Deployment/shop/worker"
        assert "container worker: env BATCH_SIZE 500 -> 2500" in f["suspects"][0]["details"]

    def test_an_old_object_changed_now_is_still_a_suspect(self):
        # kubectl get omits managedFields: a modified Deployment created a day ago
        # only offers its creationTimestamp. The change must still be dated to the
        # poll window that saw it, or it would fall out of the suspect window.
        c = shop()
        state = learned(c)
        start = c.now
        c.set_env("worker", "BATCH_SIZE", "2500")
        c.crash("worker")
        f = by_subject(run(c, state, 3))["Deployment/shop/worker"]
        change = f["suspects"][0]
        assert start - 10 <= change["at"] <= start + 10
        assert change["seconds_before"] <= 30

    def test_the_object_an_error_names_is_the_suspect(self):
        # Aurora Lab scenario 14: a ResourceQuota, then a rollout restart that the quota blocks.
        c = shop()
        state = learned(c)
        c.objects["resourcequotas"].append({"kind": "ResourceQuota", "metadata": {
            "name": "compute-resources", "namespace": "shop", "creationTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {"hard": {"requests.memory": "200Mi"}}})
        poll(c, state)
        c.set_env("api", "RESTARTED", "now")
        for i in range(3):
            c.warning("ReplicaSet", f"api-{c.revision('api')}", "FailedCreate",
                      'Error creating: pods "api-x" is forbidden: exceeded quota: compute-resources', count=i + 1)
            found = poll(c, state)
        f = state["open"]["Deployment/shop/api"]
        assert f["suspects"][0]["key"] == "ResourceQuota/shop/compute-resources"
        assert "named in the error" in f["suspects"][0]["relation"]
        # Pods that cannot be created page now, not at the rollout's progress deadline.
        assert f["severity"] == "high" and "cannot be created" in f["summary"]

    def test_a_progressing_rollout_is_not_stuck_but_a_stalled_one_is(self):
        c = shop()
        state = learned(c)
        c.stall("api", updated=1)  # new revision: 1 of 2 updated, then nothing moves
        stalled_polls = DEFAULTS["rollout_stuck_seconds"] // 10
        assert run(c, state, stalled_polls - 1) == []
        found = by_subject(run(c, state, 4))
        assert found["Deployment/shop/api"]["symptoms"][0]["kind"] == "rollout_stuck"

    def test_readiness_flapping_does_not_keep_a_stalled_rollout_looking_alive(self):
        # Pods going unready and ready again is not progress: the outage pages now,
        # not at the rollout's deadline (or never, as when every flap reset the clock).
        c = shop()
        c.scale("api", 4)
        c.deployments["api"]["strategy"] = {"type": "RollingUpdate"}
        state = learned(c)
        c.stall("api", updated=1)
        poll(c, state)
        found = []
        for i in range(4):
            c.scale("api", 4, unready=4 if i % 2 == 0 else 3)
            found += poll(c, state)
        assert "Deployment/shop/api" in by_subject(found)

    def test_a_replaced_pod_is_recorded_as_a_change_but_a_rollout_is_not(self):
        c = shop()
        state = learned(c)
        c.replace_pod("frontend")
        poll(c, state)
        assert [ch["action"] for ch in state["changes"]] == ["pod_replaced"]
        assert "other replica(s) kept running" in state["changes"][0]["details"][0]
        c.set_env("frontend", "X", "1")  # a rollout swaps pods under a new revision
        poll(c, state)
        assert [ch["action"] for ch in state["changes"]][:1] == ["changed"]

    def test_a_configmap_edit_outranks_the_restart_that_applied_it(self):
        # Aurora Lab scenario 05: a mistyped nginx directive, then `kubectl rollout restart`.
        c = shop()
        state = learned(c)
        c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8080; } proxy_conect_timeout 2s;"})
        c.restart("frontend")
        c.crash("frontend", reason="Error", exit_code=1)
        f = by_subject(run(c, state, 3))["Deployment/shop/frontend"]
        assert [s["key"] for s in f["suspects"][:2]] == ["ConfigMap/shop/frontend-nginx", "Deployment/shop/frontend"]
        assert f["suspects"][1]["details"][0].startswith("rollout restart requested")

    def test_a_configmap_edit_outranks_the_replaced_pod_that_picked_it_up(self):
        # Aurora Lab scenario 19: the ConfigMap is edited and one of two pods is deleted.
        c = shop()
        state = learned(c)
        c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8000; }"})
        c.replace_pod("frontend")
        c.crash("frontend", reason="Error", exit_code=1)
        f = by_subject(run(c, state, 3))["Deployment/shop/frontend"]
        assert [s["action"] for s in f["suspects"][:2]] == ["changed", "pod_replaced"]
        assert f["suspects"][0]["key"] == "ConfigMap/shop/frontend-nginx"

    def test_a_restart_with_nothing_else_changed_is_still_the_suspect(self):
        c = shop()
        state = learned(c)
        c.restart("api")
        c.crash("api", reason="Error", exit_code=1)
        f = by_subject(run(c, state, 3))["Deployment/shop/api"]
        assert f["suspects"][0]["key"] == "Deployment/shop/api"
        assert f["suspects"][0]["relation"] == "same object (a restart)"

    def test_a_shared_configmap_edit_outranks_the_restart_of_the_workload_it_broke(self):
        # api-code is shared: the api restarts first and is fine; the worker restarts
        # minutes later and crash-loops. The edit is still the worker's cause.
        c = shop()
        state = learned(c)
        c.configmap("api-code", {"api.py": "print('api')", "worker.py": "raise SystemExit(1)"})
        poll(c, state, seconds=60)
        c.restart("api")
        for _ in range(4):
            poll(c, state, seconds=60)
        c.restart("worker")
        c.crash("worker", reason="Error", exit_code=1)
        f = by_subject(run(c, state, 3, seconds=60))["Deployment/shop/worker"]
        assert f["suspects"][0]["key"] == "ConfigMap/shop/api-code"

    def test_a_configmap_edit_outranks_a_restart_made_minutes_later(self):
        # Edit the ConfigMap, see nothing reload, restart five minutes later: the edit is
        # the cause, put into effect by the restart.
        c = shop()
        state = learned(c)
        c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8000; }"})
        assert run(c, state, 30) == []
        c.restart("frontend")
        c.crash("frontend", reason="Error", exit_code=1)
        f = by_subject(run(c, state, 3))["Deployment/shop/frontend"]
        assert [s["key"] for s in f["suspects"][:2]] == ["ConfigMap/shop/frontend-nginx", "Deployment/shop/frontend"]


class TestTwoFaultsAtOnce:
    """Aurora Lab scenario 12: the alert only covered the worker, and the RCA called the frontend healthy."""

    def test_two_changes_make_two_incidents_each_blaming_its_own_change(self):
        c = shop()
        state = learned(c)
        c.set_env("worker", "BATCH_SIZE", "2500")
        c.crash("worker")
        c.set_selector("frontend", {"app": "frontend", "tier": "web"})
        logs = healthy_logs()
        logs["Deployment/shop/loadgen"] = ["2026-11-17T22:00:14Z GET /api/items -> timeout"] * 3
        found = run(c, state, 3, logs)
        groups = [g for g in group_findings(found) if g["opens_incident"]]
        primaries = sorted(g["primary_suspect"]["key"] for g in groups)
        assert primaries == ["Deployment/shop/worker", "Service/shop/frontend"]
        frontend = next(g for g in groups if g["primary_suspect"]["key"] == "Service/shop/frontend")
        # The clients timing out are a symptom of the frontend fault, not a third incident.
        assert {f["subject"] for f in frontend["findings"]} == {"Service/shop/frontend", "Deployment/shop/loadgen"}


class TestOneChangeOneIncident:
    def test_a_configmap_edit_that_breaks_pods_and_their_service_is_one_incident(self):
        c = shop()
        state = learned(c)
        c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8080; } proxy_conect_timeout 2s;"})
        c.crash("frontend", reason="Error", exit_code=1)
        found = run(c, state, 4)
        subjects = {f["subject"] for f in found}
        assert {"Deployment/shop/frontend", "Service/shop/frontend"} <= subjects
        groups = [g for g in group_findings(found) if g["opens_incident"]]
        assert len(groups) == 1
        assert groups[0]["primary_suspect"]["key"] == "ConfigMap/shop/frontend-nginx"


class TestCascades:
    """Aurora Lab scenario 09: redis loses its volume; the api that depends on it crash-loops."""

    def test_a_workload_failing_because_its_dependency_is_down_joins_that_incident(self):
        c = shop()
        state = learned(c)
        c.set_env("redis", "DATA_DIR", "/data")  # stands in for the PVC change
        c.crash("redis", reason="Error", exit_code=1)
        c.set_env("api", "LOG_LEVEL", "debug")  # an unrelated change on the api, same minute
        c.crash("api", reason="Error", exit_code=1)  # REDIS_HOST=redis: it cannot start without redis
        found = run(c, state, 4)
        assert {"Deployment/shop/redis", "Service/shop/redis", "Deployment/shop/api"} <= {f["subject"] for f in found}
        groups = [g for g in group_findings(found) if g["opens_incident"]]
        keys = {g["key"] for g in groups}
        api = next(f for f in found if f["subject"] == "Deployment/shop/api")
        redis = next(f for f in found if f["subject"] == "Deployment/shop/redis")
        assert api["incident_key"] == redis["incident_key"]
        assert redis["incident_key"] in keys and "Deployment/shop/redis" in redis["incident_key"]


class TestDependencyConfig:
    def test_a_dependency_s_config_edit_outranks_the_restart_that_applied_it(self):
        c = Cluster()
        c.configmap("api-code", {"api.py": "print('api')", "worker.py": "print('worker')"})
        c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8080; }"})
        c.configmap("redis-conf", {"redis.conf": "port 6379"})
        c.deployment("redis", configmap="redis-conf")
        c.deployment("api", replicas=2, env={"REDIS_HOST": "redis"}, configmap="api-code")
        c.deployment("worker", env={"BATCH_SIZE": "500"}, configmap="api-code")
        c.deployment("frontend", replicas=2, configmap="frontend-nginx")
        c.deployment("loadgen")
        c.service("redis", {"app": "redis"}, 6379, 6379)
        c.service("api", {"app": "api"}, 8080, 8080)
        c.service("frontend", {"app": "frontend"}, 80, 80)
        state = learned(c)
        c.configmap("redis-conf", {"redis.conf": "port 6380"})
        assert run(c, state, 30) == []  # nothing reloads it for five minutes
        c.restart("redis")  # the restart puts it into effect, and the api cannot connect
        c.crash("api", reason="Error", exit_code=1)
        logs = healthy_logs()
        logs["Deployment/shop/api"] = ["redis.exceptions.ConnectionError: Error 111 connecting to redis:6379."] * 3
        f = by_subject(run(c, state, 3, logs))["Deployment/shop/api"]
        assert f["suspects"][0]["key"] == "ConfigMap/shop/redis-conf"


class TestLogs:
    def test_new_request_failures_page_but_new_internal_errors_do_not(self):
        c = shop()
        state = learned(c)
        logs = healthy_logs()
        logs["Deployment/shop/loadgen"] = ["GET /api/items -> 502"] * 3 + ["GET /api/items -> 200"]
        logs["Deployment/shop/worker"] = ["ERROR metrics: push to pushgateway:9091 failed: [Errno -2] Name does not resolve"] * 2
        found = by_subject(run(c, state, 2, logs))
        assert found["Deployment/shop/loadgen"]["severity"] == "high"
        assert found["Deployment/shop/worker"]["severity"] == "low"
        incidents = [g for g in group_findings(list(found.values())) if g["opens_incident"]]
        assert [g["findings"][0]["subject"] for g in incidents] == ["Deployment/shop/loadgen"]

    def test_credentials_in_log_lines_are_redacted_before_they_are_quoted(self):
        c = shop()
        state = learned(c)
        logs = healthy_logs()
        fake_pat = "ghp_" + "q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i"  # synthetic; split so none is committed whole
        leak = f"request failed: upstream auth error token={fake_pat}"
        logs["Deployment/shop/api"] = logs["Deployment/shop/api"] + [leak] * 3
        found = by_subject(run(c, state, 2, logs))
        evidence = " ".join(found["Deployment/shop/api"]["evidence"])
        assert "request failed" in evidence
        assert fake_pat not in evidence
        # Not even a digit-free fragment of it, which masking digits alone would leave.
        stored = json.dumps(state)
        assert "TmRzW" not in stored and "cDhG" not in stored

    def test_tokens_in_successful_requests_never_reach_a_stored_template(self):
        # 2xx lines are learned every poll, so their template must not carry the token.
        c = shop()
        state = new_state()
        jwt = ("eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJ1c2VyLTQyIiwiZW1haWwiOiJhbGljZUBleGFtcGxlLmNvbSJ9"
               ".QmFzZTY0VXJsU2lnbmF0dXJl")
        logs = healthy_logs()
        logs["Deployment/shop/frontend"] = [
            f'10.244.0.9 - - [17/Nov/2026:21:44:51 +0000] "GET /ws/updates?access_token={jwt} HTTP/1.1" 200 38'] * 3
        for _ in range(LEARN + 2):
            poll(c, state, logs)
        stored = json.dumps(state)
        assert "ImtpZCI" not in stored and "VXJsU" not in stored

    def test_what_is_kept_about_a_deleted_workload_is_dropped(self):
        c = shop()
        state = learned(c)
        logs = healthy_logs()
        logs["Deployment/shop/worker"] = ["ERROR metrics: push failed"] * 2
        poll(c, state, logs)
        assert any(k.startswith("Deployment/shop/worker|") for k in state["pending"])
        del c.deployments["worker"]  # a preview environment torn down
        poll(c, state)
        assert not any(k.startswith("Deployment/shop/worker|") for k in list(state["pending"]) + list(state["held"]))

    def test_lines_from_before_the_first_poll_are_history_not_baseline(self):
        # Startup errors fetched by the first poll must not teach "timeouts are normal".
        c = shop()
        state = new_state()
        startup = healthy_logs()
        startup["Deployment/shop/loadgen"] = ["GET /api/items -> timeout"] * 5
        poll(c, state, startup)
        for _ in range(LEARN - 1):
            poll(c, state)
        logs = healthy_logs()
        logs["Deployment/shop/loadgen"] = ["GET /api/items -> timeout"] * 3
        found = by_subject(run(c, state, 2, logs))
        assert found["Deployment/shop/loadgen"]["severity"] == "high"

    def test_failures_with_no_success_left_are_flagged_but_silence_is_not(self):
        c = shop()
        state = learned(c)
        quiet = healthy_logs()
        quiet["Deployment/shop/loadgen"] = []  # no traffic at night
        assert run(c, state, 4, quiet) == []

    def test_known_error_rate_surge(self):
        c = shop()
        state = new_state()
        noisy = healthy_logs()
        noisy["Deployment/shop/api"] = ['10.0.0.1 "GET /items HTTP/1.0" 200 -', "request failed: redis error: timed out"]
        for _ in range(LEARN):
            poll(c, state, noisy)
        surge = dict(noisy)
        surge["Deployment/shop/api"] = ['10.0.0.1 "GET /items HTTP/1.0" 200 -'] + ["request failed: redis error: timed out"] * 40
        found = by_subject(run(c, state, 2, surge))
        kinds = {s["kind"] for s in found["Deployment/shop/api"]["symptoms"]}
        assert "error_log_surge" in kinds

    def test_logs_first_read_after_learning_are_learned_before_they_count(self):
        # More workloads than one poll reads logs for: the worker's were not read while
        # learning. Its chronic error line is its baseline, not a new failure.
        c = shop()
        logs = healthy_logs()
        unread = {k: v for k, v in logs.items() if k != "Deployment/shop/worker"}
        state = new_state()
        for _ in range(LEARN):
            poll(c, state, unread)
        logs["Deployment/shop/worker"] = ["ERROR dial tcp 10.0.0.9:5432: connect: connection refused (retrying)"] * 2
        assert run(c, state, LEARN + 2, logs) == []
        logs["Deployment/shop/worker"] += ["upstream timed out while connecting to upstream"] * 3
        assert "Deployment/shop/worker" in by_subject(run(c, state, 2, logs))

    def test_after_an_incident_resolves_a_surge_of_its_error_still_pages(self):
        c = shop()
        state = learned(c)
        failing = healthy_logs()
        failing["Deployment/shop/api"] = ['10.0.0.1 "GET /items HTTP/1.0" 200 -'] + [
            "upstream timed out while connecting to upstream"] * 2
        run(c, state, 30, failing)  # 2 a poll for a while: an incident
        assert "Deployment/shop/api" in state["open"]
        run(c, state, DEFAULTS["hold_seconds"] // 10 + DEFAULTS["resolve_polls"] + 1)
        assert "Deployment/shop/api" not in state["open"]
        surge = healthy_logs()
        surge["Deployment/shop/api"] = ['10.0.0.1 "GET /items HTTP/1.0" 200 -'] + [
            "upstream timed out while connecting to upstream"] * 25
        found = by_subject(run(c, state, 2, surge))
        assert {s["kind"] for s in found["Deployment/shop/api"]["symptoms"]} >= {"error_log_surge"}

    def test_the_line_mask_takes_quoted_json_and_header_credentials_and_keeps_signals(self):
        from services.watch.detector import _mask_secrets
        from services.watch.logs import normalize

        pw, key = "Sup3rS3cret" + "Passw0rd", "Zk3pQ9" + "vX2mL7tR4w"  # synthetic; split so none is committed whole
        assert "Sup3rS3cret" not in _mask_secrets('{"level":"error","password":"' + pw + '"}')
        assert "Zk3pQ9" not in _mask_secrets('msg="partner call ok" api_key="' + key + '"')
        assert "abc123" not in _mask_secrets("Authorization: Bearer abc123def")
        # An HTTP status after a credential-shaped pair, pipe-delimited, is still a status.
        line = "2026-11-17 21:44:51|ERROR|session_id=web-1|status=500|path=/items"
        assert normalize(_mask_secrets(line)).http_class == "5xx"
        # Words that only look like keys, and lowercase names, are left alone.
        for kept in ("transcoder exited: signal: killed", "auth=failed",
                     "connect to checkout-service-v2-canary refused", "payments-6c5d4f7b8c-a1b2c"):
            assert _mask_secrets(kept) == kept

    def test_ids_are_masked_but_names_are_not(self):
        from services.watch.detector import _mask_secrets

        for request_id in ("ckx7q2m9a0001ab3cd4ef5gh", "job_01hq3k8x9yz2w4v6t8r0p2n4m6", "sess-k3j4h5g6f7d8s9a0q1w2"):
            assert "<token>" in _mask_secrets(f"ERROR job {request_id} attempt 2 failed"), request_id
        for name in ("checkout-service-v2-canary", "payments-6c5d4f7b8c-a1b2c", "connection-timeout-after-30000ms"):
            assert _mask_secrets(f"connect to {name} refused") == f"connect to {name} refused"

    def test_a_chronic_error_naming_a_new_id_each_time_is_not_new(self):
        # A worker retries failing jobs and logs each job's id; that was its normal while
        # learning, so new ids after learning are not new errors.
        import hashlib
        import itertools

        c = shop()
        state = new_state()
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        ids = ("c" + "".join(alphabet[b % 36] for b in hashlib.sha256(str(n).encode()).digest()[:24])
               for n in itertools.count())  # cuid-like: letters and digits vary per id
        def logs():
            out = healthy_logs()
            out["Deployment/shop/worker"] = [f"ERROR job {next(ids)} attempt {a} failed: timeout" for a in (1, 2)] * 2
            return out
        for _ in range(LEARN):
            poll(c, state, logs())
        found = []
        for _ in range(6):
            found += poll(c, state, logs())
        assert found == []

    def test_the_line_mask_takes_values_with_spaces_escaped_json_and_query_credentials(self):
        from services.watch.detector import _mask_secrets

        pw = "correct horse " + "battery staple"  # synthetic
        assert "horse" not in _mask_secrets('{"user":"svc","passphrase":"' + pw + '"}')
        assert "horse" not in _mask_secrets('err="bad body {\\"password\\":\\"' + pw + '\\"}"')
        for url in ("GET /sync?sig=Zk3pQ9vXm2L7&se=2026", "GET /db?pass=hunter2", "GET /x?auth=AIzaSyA123"):
            masked = _mask_secrets(url)
            assert "hunter2" not in masked and "Zk3pQ9" not in masked and "AIzaSyA" not in masked, url
        assert _mask_secrets("token: expired") == "token: expired"  # prose keeps its word

    def test_the_line_mask_keeps_a_word_after_a_colon_only_when_it_is_a_status(self):
        from services.watch.detector import _mask_secrets

        pw = "change" + "me"  # synthetic, and digit-free like many real passwords
        for line in (f"config {{User:shop Password:{pw} DB:0}}", f"  password: {pw}", f"ClientSecret:{pw}",
                     f"credentials=alerts:{pw}", f'"credentials":"robot:{pw}"', f"?credentials=a:{pw}&x=1",
                     f"login rejected password=sunshine2024!{pw}", f"bind failed password=Zx9\\{pw}"):
            assert pw not in _mask_secrets(line), line
        for kept in ("token: expired", "using password: YES", "invalid credentials: expired."):
            assert _mask_secrets(kept) == kept

    def test_a_query_string_in_a_json_log_line_ends_where_the_url_does(self):
        # The fields after the URL keep their words, so the line is still a failing request.
        line = ('{"time":"2026-11-18T05:00:00Z","method":"GET","uri":"/api/orders?page_token=abc123",'
                '"status":503,"error":"upstream timed out"}')
        (entry,) = template_counts([line]).values()
        assert entry["request_failure"] and "upstream timed out" in entry["sample"]
        assert "abc123" not in entry["sample"]

    def test_samples_quoted_as_evidence_go_through_the_full_secret_rules(self):
        c = shop()
        state = learned(c)
        logs = healthy_logs()
        fake_pat = "ghp_" + "q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i"  # synthetic; split so none is committed whole
        logs["Deployment/shop/api"] = logs["Deployment/shop/api"] + [
            f"request failed: upstream rejected credentials password=hunter2 github={fake_pat[:-4]}"] * 3
        found = by_subject(run(c, state, 2, logs))
        evidence = " ".join(found["Deployment/shop/api"]["evidence"])
        assert "hunter2" not in evidence and "q83Xa2" not in evidence

    def test_hostile_log_lines_cost_little(self):
        # A client can make every access-log line 4 KB of keyword soup; a poll must not
        # spend a minute on it.
        import time

        prefix = '10.0.0.1 - - [17/Nov/2026:21:44:51 +0000] "GET / HTTP/1.1" 200 38 "-" "'
        for soup in ("key" * 1300, "a." * 1950, "a-b." * 975, "password=" * 440):
            start = time.perf_counter()
            template_counts([prefix + soup + '"'] * 640)
            assert time.perf_counter() - start < 3, soup[:12]


class TestEvents:
    def test_a_warning_that_fired_all_through_learning_is_background_noise(self):
        c = shop()
        state = new_state()
        for i in range(LEARN):
            c.warning("Pod", c.pod_name("api", 0), "Unhealthy", "Readiness probe failed", count=i + 1)
            poll(c, state)
        for i in range(LEARN, LEARN + 4):
            c.warning("Pod", c.pod_name("api", 0), "Unhealthy", "Readiness probe failed", count=i + 1)
            assert poll(c, state) == []

    def test_a_new_warning_is_a_medium_finding_that_does_not_open_an_incident(self):
        c = shop()
        state = learned(c)
        for i in range(3):
            c.warning("Pod", c.pod_name("api", 0), "BackOff", "Back-off pulling image", count=i + 1)
            found = poll(c, state)
        groups = group_findings(found or list(state["open"].values()))
        assert groups and not groups[0]["opens_incident"]
        assert groups[0]["severity"] == "medium"


class TestLifecycle:
    def test_a_finding_is_reported_once_and_resolves_after_clean_polls(self):
        c = shop()
        state = learned(c)
        c.crash("worker")
        assert len(run(c, state, 5)) == 1
        c.heal("worker")
        # Restarts still in the restart window keep it open, then clean polls close it.
        run(c, state, DEFAULTS["restart_window_seconds"] // 10)
        assert "Deployment/shop/worker" in state["open"]
        run(c, state, DEFAULTS["resolve_polls"] + 1)
        assert state["open"] == {}
        assert state["closed"][-1]["subject"] == "Deployment/shop/worker"

    def test_a_slow_crash_loop_is_confirmed_across_back_off_gaps(self):
        # Aurora Lab scenario 06: a liveness probe on the wrong port. The kubelet kills
        # the container every minute or so; no two consecutive polls see a new restart.
        c = shop()
        state = learned(c)
        found = []
        for i in range(14):
            if i in (2, 8):  # two restarts 60s apart, Warning events in between
                c.deployments["api"]["restarts"] += 1
            if i % 3 == 0:
                c.warning("Pod", c.pod_name("api", 0), "Unhealthy",
                          "Liveness probe failed: dial tcp 10.0.0.5:8081: connect: connection refused", count=i + 1)
            found += poll(c, state)
        api = [f for f in found if f["subject"] == "Deployment/shop/api"]
        assert api and max(f["severity"] for f in api) == "high"
        assert len({f["id"] for f in api}) == 1  # one finding, not a flapping series

    def test_one_restart_is_a_notice_not_an_incident(self):
        c = shop()
        state = learned(c)
        c.deployments["worker"]["restarts"] += 1
        found = run(c, state, 4)
        assert [f["severity"] for f in found] == ["medium"]
        assert not any(g["opens_incident"] for g in group_findings(found))

    def test_a_notice_that_becomes_an_outage_is_reported_again(self):
        c = shop()
        state = learned(c)
        logs = healthy_logs()
        logs["Deployment/shop/worker"] = ["ERROR metrics: push failed"] * 2 + ["processed batch of 500 items"]
        first = run(c, state, 2, logs)
        assert [f["severity"] for f in first] == ["low"]
        c.crash("worker")
        again = run(c, state, 3, logs)
        assert [f["subject"] for f in again] == ["Deployment/shop/worker"]
        assert again[0]["severity"] == "critical"

    def test_an_outage_with_a_newer_cause_is_reported_again_with_that_cause(self):
        c = shop()
        state = learned(c)
        for _ in range(2):  # two restarts in a few minutes: high, with no change to blame
            c.deployments["worker"]["restarts"] += 1
            poll(c, state)
        finding = state["open"]["Deployment/shop/worker"]
        assert finding["severity"] == "high" and finding["suspects"] == []
        finding["incident_id"] = "I1"  # what report_open_findings stores
        first_key = finding["incident_key"]
        c.set_env("worker", "BATCH_SIZE", "2500")  # a bad deploy, later, takes it down
        c.crash("worker")
        again = run(c, state, 4)
        assert [f["subject"] for f in again] == ["Deployment/shop/worker"]
        assert finding["severity"] == "critical"
        assert finding["suspects"][0]["details"] == ["container worker: env BATCH_SIZE 500 -> 2500"]
        assert "incident_id" not in finding and finding["incident_key"] != first_key

    def test_an_outage_getting_worse_for_the_same_cause_updates_its_incident(self):
        c = shop()
        state = learned(c)
        c.set_env("worker", "BATCH_SIZE", "2500")
        poll(c, state)  # the new revision's pod starts, then restarts now and then
        found = []
        for _ in range(2):
            c.deployments["worker"]["restarts"] += 1
            found += poll(c, state)
        finding = state["open"]["Deployment/shop/worker"]
        assert finding["severity"] == "high"
        assert finding["suspects"][0]["details"] == ["container worker: env BATCH_SIZE 500 -> 2500"]
        finding["incident_id"], key = "I1", finding["incident_key"]
        c.crash("worker")
        assert run(c, state, 4) == []  # not a new finding...
        assert finding["severity"] == "critical" and finding["incident_key"] == key
        assert "incident_id" not in finding  # ...but reported again, to update its incident

    def test_a_steady_restart_cadence_is_one_finding_not_one_per_cycle(self):
        c = shop()
        state = learned(c)
        found = []
        for i in range(108):  # 3 hours of a restart every 8 minutes, polled every 100s
            if i % 5 == 0:
                c.deployments["worker"]["restarts"] += 1
            found += poll(c, state, seconds=100)
        # One finding (a notice, then incident-worthy at the second restart) that stays
        # open between restarts, instead of closing and reopening every cycle.
        assert {f["id"] for f in found} == {state["open"]["Deployment/shop/worker"]["id"]}
        assert state["closed"] == []

    def test_a_second_symptom_of_the_same_outage_keeps_its_incident(self):
        # The kinds of one crash loop confirm polls apart (pods failing, then restarts):
        # more of the same outage, not a newer cause with an incident of its own.
        c = shop()
        state = learned(c)
        c.configmap("api-code", {"api.py": "print('api')", "worker.py": "raise SystemExit(1)"})
        poll(c, state, seconds=60)
        c.restart("worker")
        c.crash("worker", reason="Error", exit_code=1)
        keys = set()
        for _ in range(6):
            poll(c, state, seconds=60)
            finding = state["open"].get("Deployment/shop/worker")
            if finding:
                keys.add(finding["incident_key"])
        assert len(keys) == 1

    def test_a_newer_cause_of_a_new_symptom_is_blamed_even_without_a_rise_in_severity(self):
        # api is 1 of 2 ready (high); later a NetworkPolicy breaks its requests (high too).
        c = shop()
        state = learned(c)
        c.scale("api", 2, unready=1)
        run(c, state, 3)
        finding = state["open"]["Deployment/shop/api"]
        assert finding["severity"] == "high"
        finding["incident_id"], first_key = "I1", finding["incident_key"]
        run(c, state, 30)  # ten minutes on
        c.objects["networkpolicies"].append({"kind": "NetworkPolicy", "metadata": {
            "name": "lockdown", "namespace": "shop", "creationTimestamp": "2026-01-01T00:00:00Z"},
            "spec": {"podSelector": {"matchLabels": {"app": "api"}}, "policyTypes": ["Ingress"]}})
        logs = healthy_logs()
        logs["Deployment/shop/api"] = ["upstream timed out while connecting to upstream"] * 3
        run(c, state, 3, logs)
        assert finding["suspects"][0]["key"] == "NetworkPolicy/shop/lockdown"
        assert finding["incident_key"] != first_key and "incident_id" not in finding

    def test_a_scale_up_after_an_outage_began_is_not_a_newer_cause(self):
        # An HPA (or a person) scaling up in response is not what broke it.
        c = shop()
        state = learned(c)
        c.configmap("api-code", {"api.py": "raise SystemExit(1)", "worker.py": "print('worker')"})
        c.replace_pod("api")
        c.scale("api", 2, unready=1)
        run(c, state, 3)
        finding = state["open"]["Deployment/shop/api"]
        finding["incident_id"], key = "I1", finding["incident_key"]
        run(c, state, 12)
        c.scale("api", 3, unready=1)  # scaled up
        logs = healthy_logs()
        logs["Deployment/shop/api"] = ["upstream timed out while connecting to upstream"] * 3
        run(c, state, 3, logs)
        assert finding["incident_key"] == key

    def test_a_dependency_scaled_to_zero_is_named_as_the_newer_cause(self):
        # Scaling up answers an outage; scaling a dependency down to zero can cause one.
        c = shop()
        state = learned(c)
        for _ in range(2):
            c.deployments["api"]["restarts"] += 1
            poll(c, state)
        finding = state["open"]["Deployment/shop/api"]  # restarting, nothing to blame
        finding["incident_id"], key = "I1", finding["incident_key"]
        for i in range(60):
            if i % 24 == 0:
                c.deployments["api"]["restarts"] += 1
            poll(c, state)
        c.scale("redis", 0)
        logs = healthy_logs()
        logs["Deployment/shop/api"] = ["request failed: redis error: connection refused"] * 3
        run(c, state, 4, logs)
        assert finding["suspects"][0]["key"] == "Deployment/shop/redis"
        assert finding["incident_key"] != key

    def test_a_broken_deploy_made_during_a_polling_gap_is_named_as_the_newer_cause(self):
        c = shop()
        state = learned(c)
        c.set_env("worker", "BATCH_SIZE", "2500")
        poll(c, state)
        for _ in range(2):
            c.deployments["worker"]["restarts"] += 1
            poll(c, state)
        finding = state["open"]["Deployment/shop/worker"]
        finding["incident_id"], key = "I1", finding["incident_key"]
        c.tick(12 * 60)  # no poll for 12 minutes, and meanwhile another, broken deploy
        c.set_env("worker", "BATCH_SIZE", "99999")
        c.crash("worker", reason="OOMKilled", exit_code=137)
        run(c, state, 4)
        assert finding["suspects"][0]["details"] == ["container worker: env BATCH_SIZE 2500 -> 99999"]
        assert finding["incident_key"] != key

    def test_a_deploy_made_during_a_gap_longer_than_the_change_window_is_still_the_suspect(self):
        c = shop()
        state = learned(c)
        run(c, state, 3)
        c.tick(2 * 3600)  # the cluster unreachable for two hours
        c.set_env("worker", "BATCH_SIZE", "2500")
        c.crash("worker")
        f = by_subject(run(c, state, 4))["Deployment/shop/worker"]
        assert f["suspects"][0]["details"] == ["container worker: env BATCH_SIZE 500 -> 2500"]

    def test_a_deploy_made_during_a_polling_gap_does_not_outrank_a_later_cause(self):
        c = shop()
        state = learned(c)
        run(c, state, 3)
        c.tick(12 * 60)  # no poll for 12 minutes; a harmless deploy meanwhile
        c.set_env("frontend", "LOG_FORMAT", "json")
        assert run(c, state, 7) == []
        c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8080; } proxy_conect_timeout 2s;"})
        c.restart("frontend")
        c.crash("frontend", reason="Error", exit_code=1)
        f = by_subject(run(c, state, 3))["Deployment/shop/frontend"]
        assert f["suspects"][0]["key"] == "ConfigMap/shop/frontend-nginx"

    def test_a_deploy_made_during_a_polling_gap_is_still_the_suspect(self):
        c = shop()
        state = learned(c)
        run(c, state, 3)
        c.tick(12 * 60)  # no poll for 12 minutes (a worker backlog, a backoff)
        c.set_env("worker", "BATCH_SIZE", "2500")
        c.crash("worker")
        f = by_subject(run(c, state, 4))["Deployment/shop/worker"]
        assert f["suspects"][0]["details"] == ["container worker: env BATCH_SIZE 500 -> 2500"]

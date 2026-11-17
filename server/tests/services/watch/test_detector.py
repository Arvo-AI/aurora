"""Aurora Watch detector: regressions against a learned baseline, and who caused them.

Every test drives the real pipeline (build_snapshot -> detect_changes -> step)
over a small cluster that is broken the way Aurora Lab breaks its ``shop`` app.
"""

from __future__ import annotations

from services.watch.detector import DEFAULTS, group_findings, new_state, step
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
        state = learned(c)
        c.roll("api", unready=2)
        assert run(c, state, 8) == []

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
        leak = "request failed: upstream auth error token=ghp_q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i"
        logs["Deployment/shop/api"] = logs["Deployment/shop/api"] + [leak] * 3
        found = by_subject(run(c, state, 2, logs))
        evidence = " ".join(found["Deployment/shop/api"]["evidence"])
        assert "request failed" in evidence
        assert "ghp_q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i" not in evidence
        assert "ghp_q83Xa2nLv9P4f7Jv6TmRzW8b1K2cDhG5sY0i" not in str(state)

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
        # Recent restarts keep it open for the hold period, then clean polls close it.
        run(c, state, DEFAULTS["hold_seconds"] // 10)
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

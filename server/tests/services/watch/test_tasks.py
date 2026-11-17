"""poll_cluster end to end: kubectl output in, incidents out. DB and incident creation stubbed."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from services.watch import tasks
from services.watch.collector import KubectlError
from services.watch.detector import DEFAULTS

from .k8s import Cluster, shop

ORG, OWNER = "org-1", "user-1"
CLUSTER = {"cluster_id": "kc-1", "name": "prod-east"}


def fake_kubectl(cluster: Cluster, logs_by_app: dict[str, list[str]]):
    """A runner that answers like kubectl: the combined get, and logs per label selector."""
    seq = {"n": 0}

    def run(command: str, timeout: int) -> str:
        if command.startswith("get "):
            return cluster.kubectl_list()
        app = re.search(r"-l app=([a-z-]+)", command).group(1)
        out = []
        for line in logs_by_app.get(app, []):
            seq["n"] += 1
            ts = datetime.fromtimestamp(cluster.now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            out.append(f"[pod/{app}-abc12345-x1y2z/{app}] {ts}.{seq['n']:09d}Z {line}")
        return "\n".join(out)

    return run


@pytest.fixture
def env():
    stored: dict = {}
    opened: list = []
    failures = {"left": 0}
    settings = {"enabled": True, "owner_user_id": OWNER, "namespaces": None, "updated_at": None}

    def save_state(user_id, cluster_id, state, *, blind_spots=None, error=None):
        stored["error"] = error
        if state is not None:
            stored["state"] = state

    def open_incident(user_id, org_id, cluster, group):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("connection pool exhausted")
        opened.append(group)
        return {"incident_id": f"i-{len(opened)}", "status": "opened"}

    with patch("services.watch.store.get_settings", side_effect=lambda org: dict(settings)), \
            patch("services.watch.store.load_state", side_effect=lambda u, c: stored.get("state")), \
            patch("services.watch.store.save_state", side_effect=save_state), \
            patch("services.watch.incidents.open_incident", side_effect=open_incident), \
            patch.object(tasks, "_owner_in_org", return_value=True):
        yield {"stored": stored, "opened": opened, "settings": settings, "failures": failures}


def healthy():
    return {"loadgen": ["GET /api/items -> 200"] * 3, "frontend": ['"GET /api/items HTTP/1.1" 200 38'] * 3}


def polls(c: Cluster, run, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        out.append(tasks.poll_cluster(ORG, CLUSTER, runner=run, clock=lambda: c.now))
        c.tick(tasks.POLL_SECONDS)
    return out


def test_a_broken_service_opens_one_incident_blaming_its_change(env):
    c = shop()
    logs = healthy()
    run = fake_kubectl(c, logs)
    assert all(r["incidents"] == [] for r in polls(c, run, DEFAULTS["learning_polls"]))
    c.set_selector("frontend", {"app": "frontend", "tier": "web"})
    logs["loadgen"] = ["GET /api/items -> timeout"] * 3
    polls(c, run, 3)
    assert len(env["opened"]) == 1
    group = env["opened"][0]
    assert group["primary_suspect"]["key"] == "Service/shop/frontend"
    assert {f["subject"] for f in group["findings"]} >= {"Service/shop/frontend"}
    assert all(f.get("incident_id") == "i-1" for f in env["stored"]["state"]["open"].values()
               if f["severity"] in ("high", "critical"))


def test_a_failed_incident_open_is_retried_on_the_next_poll(env):
    c = shop()
    run = fake_kubectl(c, healthy())
    polls(c, run, DEFAULTS["learning_polls"])
    env["failures"]["left"] = 1
    c.set_selector("frontend", {"app": "frontend", "tier": "web"})
    polls(c, run, 3)
    assert len(env["opened"]) == 1  # failed once, opened on the retry, not duplicated


def test_polls_closer_than_the_interval_are_skipped(env):
    c = shop()
    run = fake_kubectl(c, healthy())
    assert tasks.poll_cluster(ORG, CLUSTER, runner=run, clock=lambda: c.now)["status"] == "ok"
    c.tick(5)
    assert tasks.poll_cluster(ORG, CLUSTER, runner=run, clock=lambda: c.now)["status"] == "too_soon"


def test_nothing_runs_when_watch_is_off(env):
    env["settings"]["enabled"] = False
    calls = []
    assert tasks.poll_cluster(ORG, CLUSTER, runner=lambda c, t: calls.append(c))["status"] == "disabled"
    assert calls == []


def test_an_unreachable_cluster_is_recorded_not_raised(env):
    def run(command, timeout):
        raise KubectlError("No active agent or kubeconfig for cluster 'kc-1'")

    assert tasks.poll_cluster(ORG, CLUSTER, runner=run)["status"] == "unreachable"
    assert "kc-1" in env["stored"]["error"]
    assert env["opened"] == []


def test_an_unreachable_chatbot_is_recorded_not_raised(env):
    # The real runner: requests raising must become a recorded error, not a crashed task.
    import requests

    from services.watch import collector

    with patch.dict("os.environ", {"CHATBOT_INTERNAL_URL": "http://chatbot:5007"}), \
            patch.object(collector, "gate_command", return_value=None), \
            patch("requests.post", side_effect=requests.ConnectionError("refused")):
        run = collector.internal_api_runner(OWNER, ORG, "kc-1")
        assert tasks.poll_cluster(ORG, CLUSTER, runner=run)["status"] == "unreachable"
    assert "unreachable" in env["stored"]["error"]

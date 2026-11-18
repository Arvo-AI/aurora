"""Collector: read-only commands, gated by policy, degrading to blind spots instead of failing."""

import json
from unittest.mock import patch

import pytest

from services.watch import collector
from services.watch.collector import (
    CommandBlocked,
    KubectlError,
    StateTooLarge,
    collect_logs,
    collect_state,
    parse_log_output,
    parse_ts_ns,
)
from services.watch.detector import DEFAULTS
from services.watch.snapshot import build_snapshot

from .k8s import shop


def test_log_timestamps_compare_as_time_not_as_strings():
    # Kubernetes trims trailing zeros: "....6265Z" is earlier than "....62650043Z",
    # though it sorts later as a string.
    early, late = "2026-11-17T22:00:14.6265Z", "2026-11-17T22:00:14.62650043Z"
    assert early > late
    assert parse_ts_ns(early) < parse_ts_ns(late)


def test_log_cursor_keeps_only_new_lines():
    text = ("[pod/api-1/api] 2026-11-17T22:00:01.5Z first\n"
            "[pod/api-2/api] 2026-11-17T22:00:02Z second\n"
            "not a kubectl log line\n")
    lines, cursor = parse_log_output(text, None)
    assert lines == ["first", "second"]
    again, cursor2 = parse_log_output(text + "[pod/api-1/api] 2026-11-17T22:00:03Z third\n", cursor)
    assert again == ["third"] and cursor2 > cursor


def test_state_is_a_few_gets_and_skips_kubernetes_own_namespaces():
    # Cluster-wide, in a few responses, each well under the kubectl-agent's frame limit.
    c = shop()
    calls = []

    def run(command, timeout):
        calls.append(command)
        return c.kubectl_get(command)

    raw, blind = collect_state(run)
    assert [cmd.split()[1] for cmd in calls] == [",".join(g) for g in collector.READ_GROUPS]
    assert all(cmd.endswith(" -A -o json") and "secrets" not in cmd for cmd in calls)
    assert blind == [] and len(raw["deployments"]["items"]) == 5 and raw["endpoints"]["items"]


def test_a_response_too_large_for_one_poll_fails_the_poll_with_a_hint():
    c = shop()
    huge = json.dumps({"kind": "List", "items": [{"kind": "Pod", "pad": "x" * (collector.MAX_STATE_BYTES + 1)}]})
    with pytest.raises(StateTooLarge, match="list the namespaces to watch"):
        collect_state(lambda cmd, t: huge if cmd.startswith("get pods ") else c.kubectl_get(cmd))

    def agent_refused(cmd, t):  # what the kubectl-agent answers instead of an oversized frame
        raise KubectlError("output too large (12000000 bytes); narrow the command, e.g. with -n <namespace>")

    with pytest.raises(StateTooLarge):
        collect_state(agent_refused)


def _kind_by_kind(c, refuse=()):
    items = json.loads(c.kubectl_list())["items"]

    def run(command, timeout):
        if "," in command:
            raise KubectlError("Error from server (Forbidden): one kind is forbidden")
        resource = command.split()[1]
        if resource in refuse:
            raise KubectlError(f"Error from server (Forbidden): {resource} is forbidden")
        kind = {v: k for k, v in collector.RESOURCE_OF_KIND.items()}.get(resource)
        return json.dumps({"items": [i for i in items if i["kind"] == kind]})

    return run


def test_a_refused_kind_becomes_a_blind_spot():
    c = shop()
    raw, blind = collect_state(_kind_by_kind(c, refuse=("events",)))
    assert len(raw["pods"]["items"]) == 7
    assert [b.split(":")[0] for b in blind] == ["events"]
    assert raw["_blind"] == ["events"]


def test_an_unreadable_essential_kind_fails_the_poll():
    # Unreadable Endpoints would read as "every Service lost its endpoints".
    with pytest.raises(KubectlError, match="endpoints"):
        collect_state(_kind_by_kind(shop(), refuse=("endpoints",)))


def test_a_blind_kind_is_carried_forward_not_read_as_deleted():
    from services.watch.detector import new_state, step
    c = shop()
    state = new_state()
    for i in range(DEFAULTS["learning_polls"] + 3):
        refuse = ("configmaps", "events") if i == DEFAULTS["learning_polls"] + 1 else ()
        raw, _ = collect_state(_kind_by_kind(c, refuse=refuse))
        state, confirmed = step(state, build_snapshot(raw, c.now), {})
        assert confirmed == []
        c.tick()
    assert state["changes"] == []  # no ConfigMap "deleted" and "created" again


def test_too_much_state_fails_the_poll_instead_of_reading_kind_by_kind(monkeypatch):
    monkeypatch.setattr(collector, "MAX_STATE_BYTES", 1000)
    calls = []
    big = json.dumps({"items": [{"kind": "Pod", "metadata": {"name": "x" * 2000}}]})
    with pytest.raises(collector.StateTooLarge):
        collect_state(lambda cmd, t: calls.append(cmd) or big)
    assert len(calls) == 1


def test_a_namespace_allowlist_is_read_namespace_by_namespace():
    c = shop()
    calls = []
    collect_state(lambda cmd, t: calls.append(cmd) or c.kubectl_list(), ["shop", "payments"])
    assert [cmd.split()[-3:] for cmd in calls] == [["payments", "-o", "json"], ["shop", "-o", "json"]]
    assert all(" -A " not in cmd for cmd in calls)


def test_label_values_that_could_inject_flags_are_not_used():
    c = shop()
    snap = build_snapshot(c.render(), c.now)
    snap["workloads"]["Deployment/shop/api"]["selector"] = {"app": "api --kubeconfig=/etc/x"}
    commands = []
    collect_logs(lambda cmd, t: commands.append(cmd) or "", snap, {}, 30)
    assert all("--kubeconfig" not in cmd for cmd in commands)
    assert len(commands) == 4  # api skipped, the other four read


def test_a_policy_refusal_stops_log_reads_and_is_reported():
    c = shop()
    snap = build_snapshot(c.render(), c.now)

    def run(command, timeout):
        raise CommandBlocked("denied by the org command policy")

    logs, blind = collect_logs(run, snap, {}, 30)
    assert logs == {} and blind == ["logs: denied by the org command policy"]


def test_failing_workloads_are_read_first_when_capped():
    c = shop()
    c.crash("worker")
    snap = build_snapshot(c.render(), c.now)
    read = []
    collect_logs(lambda cmd, t: read.append(cmd) or "", snap, {}, 30, max_workloads=1)
    assert len(read) == 1 and "app=worker" in read[0]


def test_internal_runner_refuses_blocked_commands_without_calling_the_cluster():
    with patch.dict("os.environ", {"CHATBOT_INTERNAL_URL": "http://chatbot:5007"}), \
            patch.object(collector, "gate_command", return_value="denied by the org command policy"), \
            patch("requests.post") as post:
        run = collector.internal_api_runner("user-1", "org-1", "cluster-1")
        with pytest.raises(CommandBlocked):
            run("logs -n shop -l app=api", 10)
    post.assert_not_called()


def test_internal_runner_sends_allowed_commands_with_the_internal_secret():
    with patch.dict("os.environ", {"CHATBOT_INTERNAL_URL": "http://chatbot:5007", "INTERNAL_API_SECRET": "s3"}), \
            patch.object(collector, "gate_command", return_value=None), \
            patch("requests.post") as post:
        post.return_value.ok = True
        post.return_value.json.return_value = {"success": True, "output": "{}"}
        run = collector.internal_api_runner("user-1", "org-1", "cluster-1")
        assert run("get pods -A -o json", 10) == "{}"
    url = post.call_args.args[0]
    assert url == "http://chatbot:5007/internal/kubectl/execute"
    assert post.call_args.kwargs["headers"] == {"X-Internal-Secret": "s3"}
    assert post.call_args.kwargs["json"] == {"user_id": "user-1", "cluster_id": "cluster-1",
                                             "command": "get pods -A -o json", "timeout": 10}


def test_gate_requires_read_only_and_org_policy():
    with patch("utils.security.read_only_classifier.is_read_only_command", return_value=True), \
            patch("utils.auth.command_policy.evaluate_compound_command") as policy:
        policy.return_value.allowed = False
        policy.return_value.rule_description = "Deny kubectl logs"
        assert collector.gate_command("org-1", "logs -n shop -l app=api") == "Deny kubectl logs"
        policy.return_value.allowed = True
        assert collector.gate_command("org-1", "logs -n shop -l app=api") is None
    with patch("utils.security.read_only_classifier.is_read_only_command", return_value=False):
        assert collector.gate_command("org-1", "delete pod x") == "not a read-only command"


def test_namespaces_that_together_exceed_the_poll_budget_say_so():
    each = json.dumps({"kind": "List", "items": [{"kind": "Pod", "pad": "x" * (5 * 1024 * 1024)}]})
    with pytest.raises(StateTooLarge, match="watch fewer namespaces"):
        collect_state(lambda cmd, t: each, [f"ns{i}" for i in range(8)])

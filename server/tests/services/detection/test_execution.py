"""What one run of a check may do and how it is judged.

The runner executes saved commands with the org's cloud access and nobody
watching, so the cases here are the ones where it must refuse or must not
report health it did not see.
"""

import json

import pytest

from services.detection import STATUS_ERROR, STATUS_FAIL, STATUS_PASS
from services.detection import execution as runner

_CHECK = {
    "provider": "gcp",
    "command": "kubectl get pods -n checkout --no-headers | grep -c CrashLoopBackOff",
    "assertion": {"expect": "number", "op": "eq", "value": 0},
}


def _cloud_exec_returning(monkeypatch, payload):
    import chat.backend.agent.tools.cloud_exec_tool as cloud_exec_tool
    import utils.cloud.cloud_utils as cloud_utils

    calls = []

    def fake_cloud_exec(provider, command, **kwargs):
        calls.append((provider, command, kwargs))
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr(cloud_exec_tool, "cloud_exec", fake_cloud_exec)
    monkeypatch.setattr(cloud_utils, "set_user_context", lambda **kwargs: calls.append(("context", kwargs)))
    return calls


def test_a_stored_write_command_is_never_executed(monkeypatch):
    # The row was changed after it was validated (a migration, a manual edit).
    calls = _cloud_exec_returning(monkeypatch, {"success": True, "output": "0"})
    tampered = dict(_CHECK, command="kubectl delete pods --all -n checkout")

    verdict = runner.judge(tampered, "user-1", "check-1")

    assert verdict.status == STATUS_ERROR
    assert calls == []


def test_the_command_runs_in_read_only_mode(monkeypatch):
    calls = _cloud_exec_returning(monkeypatch, {"success": True, "output": "0"})

    runner.judge(_CHECK, "user-1", "check-1")

    context = next(kwargs for name, kwargs in calls if name == "context")
    assert context["mode"] == "ask"


def test_an_onprem_check_goes_to_its_cluster_and_never_to_the_cloud_cli(monkeypatch):
    import chat.backend.agent.tools.kubectl_onprem_tool as onprem_tool

    cloud_calls = _cloud_exec_returning(monkeypatch, {"success": True, "output": "0"})
    onprem_calls = []

    def fake_onprem(command, cluster_id, **kwargs):
        onprem_calls.append((command, cluster_id))
        return json.dumps({"success": True, "output": ""})

    monkeypatch.setattr(onprem_tool, "on_prem_kubectl", fake_onprem)
    check = {
        "provider": "onprem_kubectl", "target": "cluster-7",
        "command": "kubectl get pods -n checkout --field-selector=status.phase=Failed --no-headers",
        "assertion": {"expect": "lines", "op": "eq", "value": 0},
    }

    assert runner.judge(check, "user-1", "check-1").status == STATUS_PASS
    assert onprem_calls == [(check["command"], "cluster-7")]
    assert [c for c in cloud_calls if c[0] != "context"] == []


def test_the_judge_exemption_matches_what_each_executor_gates():
    # The exemption is bound to one exact string. If it is not the string the
    # executor hands the gate, every scheduled run is blocked.
    assert runner.gated_form("onprem_kubectl", "kubectl get pods -A") == "kubectl get pods -A"
    assert runner.gated_form("onprem_kubectl", "get pods -A") == "kubectl get pods -A"
    assert runner.gated_form("gcp", "compute instances list") == "gcloud compute instances list"


def test_healthy_and_unhealthy_output(monkeypatch):
    _cloud_exec_returning(monkeypatch, {"success": True, "output": "0\n"})
    assert runner.judge(_CHECK, "user-1", "check-1").status == STATUS_PASS

    _cloud_exec_returning(monkeypatch, {"success": True, "output": "2\n"})
    verdict = runner.judge(_CHECK, "user-1", "check-1")
    assert verdict.status == STATUS_FAIL
    assert verdict.observed == "2"


@pytest.mark.parametrize("payload", [
    {"success": False, "error": "Failed to setup GCP environment", "output": "0"},
    {"success": False, "code": "POLICY_DENIED", "error": "Command blocked by org policy"},
    "not json",
    [],
])
def test_a_command_that_did_not_run_is_an_error_even_if_it_carries_output(monkeypatch, payload):
    # Lost access must not look like "zero crashlooping pods".
    _cloud_exec_returning(monkeypatch, payload)
    verdict = runner.judge(_CHECK, "user-1", "check-1")
    assert verdict.status == STATUS_ERROR
    # Marked as not having run: this, not unreadable output, is what "Aurora
    # has lost access" is counted from.
    assert not verdict.ran


def test_output_that_ran_but_cannot_be_read_does_not_look_like_lost_access(monkeypatch):
    _cloud_exec_returning(monkeypatch, {"success": True, "output": "three pods"})
    verdict = runner.judge(_CHECK, "user-1", "check-1")
    assert verdict.status == STATUS_ERROR
    assert verdict.ran


def test_an_exception_while_executing_is_an_error(monkeypatch):
    import chat.backend.agent.tools.cloud_exec_tool as cloud_exec_tool
    import utils.cloud.cloud_utils as cloud_utils

    def boom(*args, **kwargs):
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(cloud_exec_tool, "cloud_exec", boom)
    monkeypatch.setattr(cloud_utils, "set_user_context", lambda **kwargs: None)

    assert runner.judge(_CHECK, "user-1", "check-1").status == STATUS_ERROR

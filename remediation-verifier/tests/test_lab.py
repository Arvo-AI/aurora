"""The LIVE half: the argv, the environment and the failure it reports.

The fake binary is a real subprocess, so these tests cover the layer the other
runner tests skip: how a command is assembled, which environment it inherits,
and how a non-zero exit or a timeout becomes a LiveError. The session tests
replace the kubectl object, so no cluster and no Docker are needed either way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from simulated_cluster import FULL_STEPS, SimulatedCluster
from simulated_session import CAPTURED_AT, RecordingSleep
from verifier.contract import load_contract
from verifier.kubectl import Kubectl, LiveError, RolloutTimeout, parse_wget, probe_in_cluster
from verifier.lab import Lab, LiveSession, endpoint_addresses, fix_step_args
from verifier.observations import MODE_LIVE

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake kubectl is a POSIX script")

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys, time
record = os.environ["RV_RECORD"]
with open(record, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "kubeconfig": os.environ.get("KUBECONFIG")}))
    handle.write("\\n")
time.sleep(float(os.environ.get("RV_SLEEP", "0")))
sys.stdout.write(os.environ.get("RV_OUTPUT", "{}") + "\\n")
if os.environ.get("RV_STDERR"):
    sys.stderr.write(os.environ["RV_STDERR"] + "\\n")
sys.exit(int(os.environ.get("RV_EXIT", "0")))
"""

EMPTY_SLICE = {"items": [{"endpoints": []}]}
ONE_ADDRESS = {"items": [{"endpoints": [{"addresses": ["10.244.0.11"]}]}]}
ENDPOINTS_ARGS = [
    "-n",
    "shop",
    "get",
    "endpointslices",
    "-l",
    "kubernetes.io/service-name=frontend",
]
ROUTE = "http://frontend.shop.svc.cluster.local/api/items"
HTTP_200 = """  HTTP/1.1 200 OK
  Server: nginx/1.27
  Content-Type: application/json
  Content-Length: 44

{"items": ["kettle", "mug", "teapot"]}
RV_EXIT:0
"""
CONNECTION_REFUSED = """wget: can't connect to remote host (10.96.45.48): Connection refused
RV_EXIT:1
"""
HTTP_503 = """  HTTP/1.1 503 Service Temporarily Unavailable
  Server: nginx/1.27
  Content-Type: text/html

<html><body>503</body></html>
RV_EXIT:8
"""


class ProbeKubectl:
    """Return a recorded wget output, or fail the way kubectl does."""

    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[list[str]] = []

    def text(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        return self.output


class RecordingKubectl:
    """Return prepared documents and record what the session asked for."""

    def __init__(self, documents: list[Any]) -> None:
        self.documents = list(documents)
        self.get_calls: list[list[str]] = []
        self.text_calls: list[list[str]] = []

    def get(self, args: list[str]) -> Any:
        self.get_calls.append(list(args))
        return self.documents.pop(0)

    def text(self, args: list[str]) -> str:
        self.text_calls.append(list(args))
        return ""


def fake_kubectl(tmp_path: Path) -> Path:
    binary = tmp_path / "kubectl"
    binary.write_text(FAKE_KUBECTL, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def read_record(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def session_with(
    monkeypatch: pytest.MonkeyPatch, documents: list[Any]
) -> tuple[LiveSession, RecordingKubectl, RecordingSleep]:
    contract = load_contract()
    recorder = RecordingKubectl(documents)
    monkeypatch.setattr(Lab, "kubectl", lambda self, timeout_s=10.0: recorder)
    sleep = RecordingSleep()
    session = LiveSession(contract, Lab(Path("/lab")), sleep=sleep, clock=lambda: CAPTURED_AT)
    return session, recorder, sleep


def test_the_global_arguments_come_before_the_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_kubectl(tmp_path)
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("RV_RECORD", str(record))
    kubectl = Kubectl(
        binary=str(binary),
        timeout_s=5.0,
        global_args=("--kubeconfig", "/lab/.kubeconfig", "--context", "kind-aurora-lab"),
    )
    assert kubectl.text(["get", "pods"]).strip() == "{}"
    assert read_record(record)[0]["argv"] == [
        "--kubeconfig",
        "/lab/.kubeconfig",
        "--context",
        "kind-aurora-lab",
        "get",
        "pods",
    ]


def test_a_non_zero_exit_becomes_a_live_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RV_RECORD", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("RV_EXIT", "3")
    monkeypatch.setenv("RV_STDERR", "boom")
    kubectl = Kubectl(binary=str(fake_kubectl(tmp_path)), timeout_s=5.0)
    with pytest.raises(LiveError, match="exited 3: boom"):
        kubectl.text(["get", "pods"])


def test_a_command_that_times_out_becomes_a_live_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RV_RECORD", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("RV_SLEEP", "5")
    kubectl = Kubectl(binary=str(fake_kubectl(tmp_path)), timeout_s=0.2)
    with pytest.raises(LiveError, match="timed out"):
        kubectl.text(["get", "pods"])


def test_the_lab_binds_kubectl_to_its_own_kubeconfig(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    kubectl = lab.kubectl()
    assert lab.kubeconfig == tmp_path / ".kubeconfig"
    assert lab.context == "kind-aurora-lab"
    assert kubectl.global_args == (
        "--kubeconfig",
        str(tmp_path / ".kubeconfig"),
        "--context",
        "kind-aurora-lab",
    )


def test_a_named_cluster_uses_its_own_kubeconfig_file(tmp_path: Path) -> None:
    lab = Lab(tmp_path, cluster="second")
    assert lab.kubeconfig.name == ".kubeconfig-second"
    assert lab.context == "kind-second"


def test_the_child_environment_replaces_the_shell_kubeconfig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBECONFIG", "/poison")
    lab = Lab(tmp_path)
    assert lab.environment()["KUBECONFIG"] == str(tmp_path / ".kubeconfig")


def test_the_lab_script_gets_the_arguments_and_the_lab_environment(tmp_path: Path) -> None:
    record = tmp_path / "lab-record.txt"
    script = tmp_path / "lab.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" >> {record}\n"
        f"printf '%s\\n' \"${{KUBECONFIG:-unset}}\" >> {record}\n",
        encoding="utf-8",
    )
    lab = Lab(tmp_path)
    lab.run(["bash", str(script), "break", "12-two-faults-oom-and-selector"], timeout_s=5)
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["break", "12-two-faults-oom-and-selector"]
    assert lines[2] == str(lab.kubeconfig)


def test_a_failing_lab_script_reports_its_stderr(tmp_path: Path) -> None:
    script = tmp_path / "lab.sh"
    script.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 4\n", encoding="utf-8")
    with pytest.raises(LiveError, match="exited 4: boom"):
        Lab(tmp_path).run(["bash", str(script)], timeout_s=5)


def test_a_contract_step_is_split_without_a_shell() -> None:
    args = fix_step_args(FULL_STEPS[0])
    assert args[:4] == ["-n", "shop", "patch", "svc"]
    assert args[-1] == '-p=[{"op":"remove","path":"/spec/selector/tier"}]'


def test_a_step_that_is_not_kubectl_is_refused() -> None:
    with pytest.raises(LiveError, match="start with kubectl"):
        fix_step_args("rm -rf /")


def test_endpoint_addresses_counts_across_slices() -> None:
    document = {
        "items": [
            {"endpoints": [{"addresses": ["a", "b"]}, {"addresses": ["c"]}]},
            {"endpoints": []},
        ]
    }
    assert endpoint_addresses(document) == 3


def test_endpoint_addresses_tolerates_another_shape() -> None:
    assert endpoint_addresses({"items": "nope"}) == 0


def test_the_probe_runs_in_the_loadgen_pod_and_keeps_the_route() -> None:
    kubectl = ProbeKubectl(HTTP_200)
    result = probe_in_cluster(kubectl, "shop", "loadgen")(ROUTE, 5)
    assert kubectl.calls[0][:7] == [
        "-n",
        "shop",
        "exec",
        "deployment/loadgen",
        "--",
        "sh",
        "-c",
    ]
    command = kubectl.calls[0][-1]
    assert "-T 5" in command
    assert ROUTE in command
    assert result["status"] == 200


def test_a_200_response_keeps_the_status_and_the_body() -> None:
    result = probe_in_cluster(ProbeKubectl(HTTP_200), "shop", "loadgen")(ROUTE, 5)
    assert result["status"] == 200
    assert json.loads(str(result["body"])) == {"items": ["kettle", "mug", "teapot"]}
    assert result["error"] is None


def test_a_connection_error_keeps_the_message() -> None:
    result = probe_in_cluster(ProbeKubectl(CONNECTION_REFUSED), "shop", "loadgen")(ROUTE, 5)
    assert result["status"] is None
    assert "Connection refused" in str(result["error"])


def test_a_503_keeps_the_status_and_reports_no_json_body() -> None:
    result = probe_in_cluster(ProbeKubectl(HTTP_503), "shop", "loadgen")(ROUTE, 5)
    assert result["status"] == 503
    assert result["body"] is None


def test_a_probe_that_cannot_exec_is_an_error_not_a_status() -> None:
    kubectl = ProbeKubectl(error=LiveError("pods not found"))
    result = probe_in_cluster(kubectl, "shop", "loadgen")(ROUTE, 5)
    assert result["status"] is None
    assert "pods not found" in str(result["error"])


def test_the_parser_keeps_the_last_status_line() -> None:
    output = '  HTTP/1.1 301 Moved Permanently\n  HTTP/1.1 200 OK\n\n{"items": []}\nRV_EXIT:0\n'
    result = parse_wget(ROUTE, output, 0.0)
    assert result["status"] == 200
    assert result["body"] == '{"items": []}'


def test_the_break_tells_the_lab_not_to_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, str] | None, float]] = []

    def fake_run(
        self: Lab, argv: list[str], *, timeout_s: float, extra_env: dict[str, str] | None = None
    ) -> str:
        calls.append((list(argv), extra_env, timeout_s))
        return ""

    monkeypatch.setattr(Lab, "run", fake_run)
    session = LiveSession(load_contract(), Lab(Path("/lab")))
    session.break_scenario(720)
    argv, extra_env, timeout_s = calls[0]
    assert argv[-1] == "12-two-faults-oom-and-selector"
    assert extra_env == {"SETTLE": "0"}
    assert timeout_s == 720


def test_the_lab_child_receives_the_extra_environment(tmp_path: Path) -> None:
    record = tmp_path / "settle.txt"
    script = tmp_path / "lab.sh"
    script.write_text(
        f"#!/usr/bin/env bash\nprintf '%s' \"$SETTLE\" > {record}\n",
        encoding="utf-8",
    )
    Lab(tmp_path).run(["bash", str(script)], timeout_s=5, extra_env={"SETTLE": "0"})
    assert record.read_text(encoding="utf-8") == "0"


def test_the_endpoint_wait_stops_as_soon_as_an_address_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, recorder, sleep = session_with(monkeypatch, [EMPTY_SLICE, ONE_ADDRESS])
    assert session.wait_endpoints(60, 2) is True
    assert recorder.get_calls == [ENDPOINTS_ARGS, ENDPOINTS_ARGS]
    assert sleep.calls == [2.0]


def test_the_endpoint_wait_gives_up_after_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, recorder, sleep = session_with(monkeypatch, [EMPTY_SLICE, EMPTY_SLICE])
    assert session.wait_endpoints(4, 2) is False
    assert len(recorder.get_calls) == 2
    assert sleep.calls == [2.0]


def test_the_rollout_wait_uses_the_contract_deployment_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, recorder, _ = session_with(monkeypatch, [])
    session.wait_rollout(120)
    assert recorder.text_calls == [
        ["-n", "shop", "rollout", "status", "deployment/worker", "--timeout=120s"]
    ]


def test_a_rollout_deadline_is_pacing_and_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = LiveError(
        "kubectl -n shop rollout status deployment/worker exited 1: "
        "error: timed out waiting for the condition"
    )
    kubectl = ProbeKubectl(error=error)
    monkeypatch.setattr(Lab, "kubectl", lambda self, timeout_s=10.0: kubectl)
    session = LiveSession(load_contract(), Lab(Path("/lab")))
    with pytest.raises(RolloutTimeout, match="did not finish in 30s"):
        session.wait_rollout(30)


def test_a_rollout_that_fails_for_another_reason_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = LiveError("kubectl -n shop rollout status deployment/worker exited 1: Forbidden")
    kubectl = ProbeKubectl(error=error)
    monkeypatch.setattr(Lab, "kubectl", lambda self, timeout_s=10.0: kubectl)
    session = LiveSession(load_contract(), Lab(Path("/lab")))
    with pytest.raises(LiveError) as caught:
        session.wait_rollout(30)
    assert not isinstance(caught.value, RolloutTimeout)


def test_the_rollout_wait_gets_its_own_command_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []
    kubectl = ProbeKubectl()

    def builder(self: Lab, timeout_s: float = 10.0) -> ProbeKubectl:
        seen.append(timeout_s)
        return kubectl

    monkeypatch.setattr(Lab, "kubectl", builder)
    session = LiveSession(load_contract(), Lab(Path("/lab")))
    session.wait_rollout(30)
    assert seen == [10.0, 35.0]


def test_a_fix_step_runs_as_kubectl_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    session, recorder, _ = session_with(monkeypatch, [])
    session.apply_fix(FULL_STEPS, 60)
    assert recorder.text_calls == [
        [
            "-n",
            "shop",
            "patch",
            "svc",
            "frontend",
            "--type=json",
            '-p=[{"op":"remove","path":"/spec/selector/tier"}]',
        ],
        ["-n", "shop", "set", "env", "deploy/worker", "BATCH_SIZE=500"],
    ]


def test_the_session_capture_is_live_and_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = SimulatedCluster("full")
    monkeypatch.setattr(Lab, "kubectl", lambda self, timeout_s=10.0: cluster)
    session = LiveSession(
        load_contract(),
        Lab(Path("/lab")),
        probe=cluster.probe,
        sleep=lambda _: None,
        clock=lambda: CAPTURED_AT,
    )
    captured = session.capture("full")
    assert captured.meta.mode == MODE_LIVE
    assert captured.meta.captured_at == CAPTURED_AT
    assert captured.is_complete

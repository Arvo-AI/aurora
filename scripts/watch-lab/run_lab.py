"""Replay Aurora Lab faults against Aurora Watch and measure what it reports.

For each scenario of https://github.com/Arvo-AI/aurora-lab this resets the
healthy ``shop`` app, lets Aurora Watch learn the cluster, injects the fault
with the scenario's own ``break.sh``, and records every finding Aurora Watch
confirms: when, how severe, and which change it blames. The detector, change
tracker and collector are imported from ``server/services/watch``, the same
code the Celery task runs; only the kubectl runner differs (local subprocess
here, the internal kubectl API in Aurora).

    KUBECONFIG=<disposable cluster> python scripts/watch-lab/run_lab.py \\
        --lab ../aurora-lab --out /tmp/watch-lab --context <kube-context> [12 07 ...]
    python scripts/watch-lab/run_lab.py --lab ../aurora-lab --out /tmp/watch-lab \\
        --context <kube-context> --healthy 600      # no fault: count false alarms
    python scripts/watch-lab/run_lab.py --report /tmp/watch-lab

Never point it at a real cluster: it deletes and recreates everything in the
``shop`` namespace and runs fault-injection scripts. It refuses to start unless
kubectl's current context is the one passed with --context and the API server
is on localhost, and checks again inside the shell that runs ``break.sh``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from services.watch.collector import collect_logs, collect_state  # noqa: E402
from services.watch.detector import group_findings, new_state, step  # noqa: E402
from services.watch.snapshot import build_snapshot  # noqa: E402

POLL_SECONDS = 10
NAMESPACE = "shop"
RESET_KINDS = ("deployments,replicasets,pods,services,configmaps,persistentvolumeclaims,"
               "networkpolicies,resourcequotas,limitranges,horizontalpodautoscalers,"
               "poddisruptionbudgets,events")

# Each scenario's faults (from scenario.yaml): one set per fault, holding the
# objects that fault's change touched. An incident "blames" a fault when its
# primary suspect is one of them. Scenario 12 has two independent faults and
# needs both; scenario 11 has a harmless change (the noisy worker) that must
# not be blamed for an incident.
EXPECTED_FAULTS = {
    "01": [{"Deployment/shop/worker"}],
    "02": [{"Deployment/shop/api"}],
    "03": [{"Deployment/shop/frontend"}],
    "04": [{"Deployment/shop/api"}],
    "05": [{"ConfigMap/shop/frontend-nginx", "Deployment/shop/frontend"}],
    "06": [{"Deployment/shop/api"}],
    "07": [{"Service/shop/api"}],
    "08": [{"Deployment/shop/worker"}],
    "09": [{"PersistentVolumeClaim/shop/redis-data", "Deployment/shop/redis"}],
    "10": [{"Deployment/shop/api"}],
    "11": [{"Service/shop/api"}],
    "12": [{"Deployment/shop/worker"}, {"Service/shop/frontend"}],
    "13": [{"NetworkPolicy/shop/redis"}],
    "14": [{"ResourceQuota/shop/compute-resources", "Deployment/shop/api"}],
    "15": [{"Deployment/shop/shop-api", "ConfigMap/shop/shop-api"}],
    "16": [{"Deployment/shop/api"}],
    "17": [{"ConfigMap/shop/api-code", "Deployment/shop/worker"}],
    "18": [{"Deployment/shop/api"}],
    "19": [{"ConfigMap/shop/frontend-nginx", "Deployment/shop/frontend"}],
    "20": [{"ConfigMap/shop/api-code", "Deployment/shop/api"}],
    "21": [{"ConfigMap/shop/api-code", "Deployment/shop/api"}],
}


def kubectl_runner(command: str, timeout: int = 60) -> str:
    proc = subprocess.run(["kubectl", *command_args(command)], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        from services.watch.collector import KubectlError
        raise KubectlError(proc.stderr.strip()[:300] or f"exit {proc.returncode}")
    return proc.stdout


def command_args(command: str) -> list[str]:
    import shlex
    return shlex.split(command)


def kubectl(*args: str, timeout: int = 120, check: bool = True) -> str:
    proc = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def assert_disposable(context: str) -> None:
    current = kubectl("config", "current-context").strip()
    server = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}").strip()
    if current != context or not server.startswith(("https://127.0.0.1:", "https://localhost:")):
        raise SystemExit(f"refusing to run: kubectl targets context={current!r} server={server!r}")


def posix_shell(context: str, env: dict) -> str:
    """A bash that sees the same kubeconfig, verified from inside that shell."""
    bash = shutil.which("bash", path=env["PATH"])
    if not bash or "system32" in bash.lower():  # Windows: System32's bash is WSL, with its own kubeconfig
        raise SystemExit(f"refusing to run break.sh with bash={bash!r}")
    probe = subprocess.run(
        [bash, "-c", "kubectl config current-context; kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'"],
        env=env, capture_output=True, text=True, timeout=30)
    lines = probe.stdout.strip().splitlines()
    if len(lines) < 2 or lines[0] != context or not lines[1].startswith(("https://127.0.0.1:", "https://localhost:")):
        raise SystemExit(f"refusing to run break.sh: its shell targets {lines!r}")
    return bash


def shell_env() -> dict:
    """Environment for break.sh. Some scripts call `python3`; where that name does
    not resolve to a real interpreter (Windows), put a shim to this one first."""
    env = dict(os.environ)
    found = shutil.which("python3")
    if found and "windowsapps" not in found.lower():
        return env
    import tempfile
    shim_dir = Path(tempfile.mkdtemp(prefix="watch-lab-shim-"))
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    return env


def reset(lab: Path) -> None:
    kubectl("-n", NAMESPACE, "delete", RESET_KINDS, "--all", "--wait=true", "--grace-period=1",
            "--ignore-not-found", timeout=300, check=False)
    kubectl("apply", "-f", str(lab / "app" / "shop.yaml"))
    kubectl("-n", NAMESPACE, "wait", "--for=condition=Available", "deploy", "--all", "--timeout=300s", timeout=330)
    for _ in range(60):
        if "-> 200" in kubectl("-n", NAMESPACE, "logs", "deploy/loadgen", "--tail=1", check=False):
            return
        time.sleep(2)
    raise RuntimeError("the shop app never became healthy")


class Watch:
    """One Aurora Watch instance over the lab namespace, polled by hand."""

    def __init__(self) -> None:
        self.state = new_state()
        self.cursors: dict[str, int] = {}

    def poll(self) -> list[dict]:
        raw, blind = collect_state(kubectl_runner, [NAMESPACE])
        snapshot = build_snapshot(raw, time.time())
        logs, log_blind = collect_logs(kubectl_runner, snapshot, self.cursors, POLL_SECONDS * 3,
                                       open_subjects=set(self.state["open"]))
        for b in blind + log_blind:
            print(f"    blind spot: {b}", flush=True)
        self.state, confirmed = step(self.state, snapshot, logs)
        return confirmed

    def learn(self) -> None:
        for _ in range(self.state["config"]["learning_polls"]):
            self.poll()
            time.sleep(POLL_SECONDS)


def run_scenario(lab: Path, scenario: Path, out: Path, context: str, watch_seconds: int) -> dict:
    print(f"=== {scenario.name}", flush=True)
    reset(lab)
    watch = Watch()
    watch.learn()
    env = shell_env()
    bash = posix_shell(context, env)
    t_break = time.time()
    proc = subprocess.run([bash, "-euo", "pipefail", "-c", "source break.sh"], cwd=scenario, env=env,
                          capture_output=True, text=True, timeout=300)
    print(f"    break.sh exit {proc.returncode} after {time.time() - t_break:.1f}s {proc.stderr.strip()[:160]}", flush=True)
    findings: list[dict] = []
    first_incident_at = None
    deadline = time.time() + watch_seconds
    while time.time() < deadline:
        for f in watch.poll():
            f["after_break_s"] = round(time.time() - t_break, 1)
            findings.append(f)
            top = f["suspects"][0] if f["suspects"] else None
            blame = f"{top['key']}: {'; '.join(top['details'][:2])}" if top else "no suspect"
            print(f"    +{f['after_break_s']:>6}s [{f['severity']}] {f['summary']}  <- {blame}", flush=True)
            if first_incident_at is None and f["severity"] in ("high", "critical"):
                first_incident_at = time.time()
        if first_incident_at and time.time() - first_incident_at > 60:
            break
        time.sleep(POLL_SECONDS)
    result = {
        "scenario": scenario.name, "break_exit": proc.returncode, "findings": findings,
        "groups": group_findings(findings), "polls": watch.state["polls"],
    }
    (out / f"{scenario.name}.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def run_healthy(lab: Path, out: Path, seconds: int) -> None:
    print(f"=== healthy control ({seconds}s, no fault)", flush=True)
    reset(lab)
    watch = Watch()
    watch.learn()
    findings, start = [], time.time()
    while time.time() - start < seconds:
        for f in watch.poll():
            f["after_start_s"] = round(time.time() - start, 1)
            findings.append(f)
            print(f"    +{f['after_start_s']:>6}s [{f['severity']}] {f['summary']}", flush=True)
        time.sleep(POLL_SECONDS)
    result = {"scenario": "healthy", "seconds": seconds, "polls": watch.state["polls"],
              "findings": findings, "groups": group_findings(findings)}
    (out / "healthy.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


def lab_alert(lab: Path | None, scenario: str) -> tuple[str, str]:
    """The alert Aurora Lab sends for a scenario, and how long that kind of rule waits.

    The ``Kube*`` alerts are kubernetes-mixin rules with ``for: 15m``; the lab's
    ``HighErrorRate`` alerts state their own window in the alert text.
    """
    import re as _re
    if lab is None:
        return "", ""
    text = (lab / "scenarios" / scenario / "scenario.yaml").read_text(encoding="utf-8")
    m = _re.search(r'^alert:\s*"(.*)"\s*$', text, _re.M)
    alert = m.group(1) if m else ""
    name = _re.split(r"[:\s\[]", alert, maxsplit=1)[0]
    if name.startswith("Kube"):
        return name, "15 min (`for: 15m`)"
    window = _re.search(r"over the last (\d+) minutes", alert)
    return name, (f"{window.group(1)} min window" if window else "")


def score(sid: str, incidents: list[dict]) -> tuple[int, int, int]:
    """(faults blamed, faults, incidents blaming something that was not injected)."""
    faults = EXPECTED_FAULTS.get(sid, [])
    tops = [(g.get("primary_suspect") or {}).get("key") for g in incidents]
    blamed = sum(1 for fault in faults if any(t in fault for t in tops))
    expected = set().union(*faults) if faults else set()
    wrong = sum(1 for t in tops if t not in expected)
    return blamed, len(faults), wrong


def report(out: Path, lab: Path | None = None) -> None:
    """Markdown table from the JSON results in ``out``."""
    rows = ["| Scenario | Alert the lab sends | That rule fires after | Aurora Watch: first incident | Incidents "
            "| Injected change named | Other incidents | Primary suspects |",
            "|---|---|---|---|---|---|---|---|"]
    totals = [0, 0, 0, 0]
    for path in sorted(out.glob("[0-9]*.json")):
        r = json.loads(path.read_text(encoding="utf-8"))
        sid = r["scenario"][:2]
        incidents = [g for g in r["groups"] if g["opens_incident"]]
        first = min((f["after_break_s"] for f in r["findings"] if f["severity"] in ("high", "critical")), default=None)
        blamed, faults, wrong = score(sid, incidents)
        totals = [totals[0] + blamed, totals[1] + faults, totals[2] + wrong, totals[3] + (first is not None)]
        tops = [(g.get("primary_suspect") or {}).get("key") or "none" for g in incidents]
        alert, waits = lab_alert(lab, r["scenario"])
        rows.append(f"| {r['scenario']} | {alert} | {waits} | {f'{first:.0f}s' if first is not None else 'none'} "
                    f"| {len(incidents)} | {blamed}/{faults} | {wrong} | {', '.join(tops) or 'none'} |")
    rows.append(f"\nDetected {totals[3]} scenarios; named {totals[0]} of {totals[1]} injected faults; "
                f"{totals[2]} incident(s) blamed something that was not injected.")
    healthy = out / "healthy.json"
    if healthy.exists():
        h = json.loads(healthy.read_text(encoding="utf-8"))
        n = sum(1 for g in h["groups"] if g["opens_incident"])
        rows.append(f"\nHealthy control: {h['seconds']}s, {h['polls']} polls, {len(h['findings'])} findings, {n} incidents.")
    print("\n".join(rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lab", type=Path, help="aurora-lab checkout")
    parser.add_argument("--out", type=Path, help="results directory")
    parser.add_argument("--context", help="kube context of the disposable cluster")
    parser.add_argument("--healthy", type=int, default=0, help="run a no-fault control for N seconds")
    parser.add_argument("--report", type=Path, help="print the table for a results directory and exit")
    parser.add_argument("scenarios", nargs="*", help="scenario id prefixes, e.g. 07 12 (default: all)")
    args = parser.parse_args()
    if args.report:
        report(args.report, args.lab)
        return
    if not (args.lab and args.out and args.context):
        parser.error("--lab, --out and --context are required")
    assert_disposable(args.context)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.healthy:
        run_healthy(args.lab, args.out, args.healthy)
        return
    scenarios = sorted(p for p in (args.lab / "scenarios").iterdir() if p.is_dir())
    if args.scenarios:
        scenarios = [p for p in scenarios if any(p.name.startswith(s) for s in args.scenarios)]
    for scenario in scenarios:
        try:
            run_scenario(args.lab, scenario, args.out, args.context, 480 if scenario.name.startswith("17") else 300)
        except Exception as exc:  # record and keep going
            print(f"    ERROR {scenario.name}: {exc}", flush=True)
            (args.out / f"{scenario.name}.error.txt").write_text(str(exc), encoding="utf-8")


if __name__ == "__main__":
    main()

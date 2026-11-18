"""Read-only collection of cluster state and recent logs for Aurora Watch.

Every command is built here, never by a model, and is a ``kubectl get`` or
``kubectl logs``. Each one still passes Aurora's read-only classifier and the
org's own command policy before it is sent, so an org that denies, say,
``kubectl logs`` gets a watch without log signals (reported as a blind spot)
rather than a policy bypass. The LLM safety judge is not consulted: there is no
model in this path to steer, and the same few commands run every minute.

Commands reach the cluster through the chatbot service's internal kubectl API,
the path the RCA agent already uses: the in-cluster kubectl-agent when one is
connected, otherwise the kubeconfig stored in Vault.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Read with one `kubectl get`. EndpointSlices are absent on purpose: the
# kubectl-agent's default RBAC grants v1 Endpoints but not discovery.k8s.io.
# Secrets are never read.
STATE_RESOURCES = (
    "pods", "deployments", "statefulsets", "daemonsets", "replicasets", "services", "endpoints",
    "events", "configmaps", "networkpolicies", "resourcequotas", "limitranges",
    "persistentvolumeclaims", "horizontalpodautoscalers", "poddisruptionbudgets",
)
RESOURCE_OF_KIND = {
    "Pod": "pods", "Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets",
    "ReplicaSet": "replicasets", "Service": "services", "Endpoints": "endpoints",
    "EndpointSlice": "endpointslices", "Event": "events", "ConfigMap": "configmaps",
    "NetworkPolicy": "networkpolicies", "ResourceQuota": "resourcequotas", "LimitRange": "limitranges",
    "PersistentVolumeClaim": "persistentvolumeclaims", "HorizontalPodAutoscaler": "horizontalpodautoscalers",
    "PodDisruptionBudget": "poddisruptionbudgets",
}
SYSTEM_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})
# Without these a snapshot would read as "everything was deleted" or "every Service
# lost its endpoints": the poll fails instead, and nothing is learned or reported.
ESSENTIAL_RESOURCES = ("pods", "deployments", "statefulsets", "daemonsets", "replicasets", "services", "endpoints")

# One response must stay under the chatbot's 10 MiB WebSocket frame for kubectl-agent
# responses (JSON escaping adds to the size on the wire); a poll reads a few of them.
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_POLL_BYTES = 32 * 1024 * 1024
# A cluster-wide read, split into smaller responses: one big kind (thousands of
# events, dashboards in ConfigMaps) does not push the rest over the frame limit.
READ_GROUPS = (
    ("pods",),
    ("deployments", "statefulsets", "daemonsets", "replicasets"),
    ("services", "endpoints", "events"),
    ("configmaps", "networkpolicies", "resourcequotas", "limitranges",
     "persistentvolumeclaims", "horizontalpodautoscalers", "poddisruptionbudgets"),
)
LOG_LIMIT_BYTES = 256 * 1024
MAX_LOG_WORKLOADS = 25
LOG_TAIL = 500
MAX_LINE_CHARS = 4000
COMMAND_TIMEOUT = 45

_LOG_LINE = re.compile(r"^\[pod/(?P<pod>[^/\]]+)/(?P<container>[^\]]+)\] (?P<ts>\S+) ?(?P<msg>.*)$")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._/-]{1,253}$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]{0,63}$")
_SAFE_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

Runner = Callable[[str, int], str]


class KubectlError(RuntimeError):
    """A kubectl command did not run or did not succeed."""


class CommandBlocked(KubectlError):
    """A command was refused by the read-only gate or the org's command policy."""


def gate_command(org_id: str | None, command: str) -> str | None:
    """Why ``kubectl <command>`` may not run for this org, or None if it may."""
    from utils.auth.command_policy import evaluate_compound_command
    from utils.security.read_only_classifier import is_read_only_command

    full = f"kubectl {command}"
    if not is_read_only_command(full):
        return "not a read-only command"
    verdict = evaluate_compound_command(org_id, full)
    if not verdict.allowed:
        return verdict.rule_description or "denied by the org command policy"
    return None


def internal_api_runner(user_id: str, org_id: str | None, cluster_id: str) -> Runner:
    """Run kubectl on a connected cluster via the chatbot service's internal API."""
    import requests

    base_url = os.getenv("CHATBOT_INTERNAL_URL")
    secret = os.getenv("INTERNAL_API_SECRET")

    def run(command: str, timeout: int = COMMAND_TIMEOUT) -> str:
        if not base_url:
            raise KubectlError("CHATBOT_INTERNAL_URL is not configured")
        reason = gate_command(org_id, command)
        if reason:
            raise CommandBlocked(reason)
        try:
            response = requests.post(
                f"{base_url}/internal/kubectl/execute",
                json={"user_id": user_id, "cluster_id": cluster_id, "command": command, "timeout": timeout},
                headers={"X-Internal-Secret": secret or ""},
                timeout=timeout + 15,
            )
            if not response.ok:
                raise KubectlError(f"internal kubectl API returned HTTP {response.status_code}")
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise KubectlError(f"internal kubectl API unreachable ({type(exc).__name__})") from exc
        if not result.get("success"):
            raise KubectlError((result.get("error") or "kubectl failed")[:300])
        return result.get("output") or ""

    return run


def _split_list(data: dict[str, Any], resources: tuple[str, ...] = STATE_RESOURCES
                ) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """A ``kind: List`` of mixed kinds, split by kubectl resource name."""
    raw: dict[str, dict[str, list[dict[str, Any]]]] = {r: {"items": []} for r in resources}
    for item in data.get("items") or []:
        resource = RESOURCE_OF_KIND.get(item.get("kind"))
        if resource in raw:
            raw[resource]["items"].append(item)
    return raw


class StateTooLarge(KubectlError):
    """The watched namespaces return more than one poll may read."""


_TOO_LARGE = (f"cluster state is too large to read every minute (over {MAX_STATE_BYTES // (1024 * 1024)} MB "
              "in one response); list the namespaces to watch")
_OVER_BUDGET = (f"the watched namespaces return over {MAX_POLL_BYTES // (1024 * 1024)} MB together, more than "
                "one poll reads; watch fewer namespaces")


class _NotJson(ValueError):
    def __init__(self, size: int) -> None:
        super().__init__("kubectl output is not JSON")
        self.size = size


def _read(run: Runner, command: str, budget: dict[str, int]) -> dict[str, Any]:
    try:
        text = run(command, COMMAND_TIMEOUT)
    except KubectlError as exc:
        if "output too large" in str(exc):  # the kubectl-agent refused to send it
            raise StateTooLarge(_TOO_LARGE) from exc
        raise
    size = len(text.encode("utf-8"))
    if size > MAX_STATE_BYTES:
        raise StateTooLarge(_TOO_LARGE)
    budget["left"] = budget.get("left", MAX_POLL_BYTES) - size  # every response counts, parsed or not
    if budget["left"] < 0:
        raise StateTooLarge(_OVER_BUDGET)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise _NotJson(size) from exc


def _collect_scope(run: Runner, scope: str, blind: dict[str, str], budget: dict[str, int],
                   groups: tuple[tuple[str, ...], ...] = (STATE_RESOURCES,)
                   ) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Every kind for one scope (``-A`` or ``-n <ns>``): one get per group of kinds, and a
    group the cluster refuses (an RBAC rule missing for one kind) kind by kind."""
    raw: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for group in groups:
        try:
            raw.update(_split_list(_read(run, f"get {','.join(group)} {scope} -o json", budget), group))
            continue
        except CommandBlocked:
            raise
        except StateTooLarge as exc:
            # One response too big for several kinds: read them one at a time. A single
            # kind still too big, or the poll's budget spent, ends the poll.
            if len(group) == 1 or str(exc) != _TOO_LARGE:
                raise
            logger.info("[AuroraWatch] get %s is over one response; reading kinds one by one", ",".join(group))
        except (KubectlError, ValueError) as exc:
            logger.info("[AuroraWatch] get %s failed (%s); reading kinds one by one", ",".join(group),
                        type(exc).__name__)
        for resource in group:
            try:
                raw[resource] = {"items": _read(run, f"get {resource} {scope} -o json", budget).get("items") or []}
            except (CommandBlocked, StateTooLarge):
                raise
            except (KubectlError, ValueError) as exc:
                if resource in ESSENTIAL_RESOURCES:
                    raise KubectlError(f"cannot read {resource}: {str(exc)[:200]}") from exc
                raw[resource] = {"items": []}
                blind[resource] = str(exc)[:120]
    return raw


def collect_state(run: Runner, namespaces: list[str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Cluster objects for the watched namespaces, plus the signals that could not be read.

    ``namespaces`` limits the watch (each one is read on its own, so an allowlist
    also bounds what is downloaded); ``None`` means every namespace except
    Kubernetes' own. If the cluster refuses the combined ``get`` (for example an
    RBAC rule missing for one kind), each kind is read separately. A kind that
    still cannot be read is listed in ``raw["_blind"]`` so the detector carries
    its previous state forward instead of reading it as "everything was deleted";
    if an essential kind cannot be read, or the state is too large, the poll fails.
    """
    wanted = sorted({n for n in namespaces or [] if _SAFE_NAMESPACE.match(n)})
    blind: dict[str, str] = {}
    budget: dict[str, int] = {"left": MAX_POLL_BYTES}
    if wanted:
        raw: dict[str, Any] = {r: {"items": []} for r in STATE_RESOURCES}
        for ns in wanted:
            try:
                scope_raw = _collect_scope(run, f"-n {ns}", blind, budget)
            except StateTooLarge as exc:
                if str(exc) != _TOO_LARGE:
                    raise
                # One big namespace: read it in the smaller groups a cluster-wide read uses.
                try:
                    scope_raw = _collect_scope(run, f"-n {ns}", blind, budget, READ_GROUPS)
                except StateTooLarge as again:
                    if str(again) != _TOO_LARGE:
                        raise
                    raise StateTooLarge(
                        f"namespace {ns} has over {MAX_STATE_BYTES // (1024 * 1024)} MB of one kind, more "
                        "than one poll reads; remove it from the namespaces to watch") from again
            for resource, value in scope_raw.items():
                raw.setdefault(resource, {"items": []})["items"] += value["items"]
    else:
        raw = _collect_scope(run, "-A", blind, budget, READ_GROUPS)
        for value in raw.values():
            value["items"] = [i for i in value.get("items") or []
                              if (i.get("metadata") or {}).get("namespace") not in SYSTEM_NAMESPACES]
    raw["_blind"] = sorted(blind)
    return raw, [f"{resource}: {msg}" for resource, msg in sorted(blind.items())]


def _selector_arg(selector: dict[str, str]) -> str | None:
    parts = []
    for k, v in sorted(selector.items()):
        if not (_SAFE_LABEL.match(str(k)) and _SAFE_VALUE.match(str(v))):
            return None
        parts.append(f"{k}={v}")
    return ",".join(parts) or None


def parse_ts_ns(ts: str) -> int | None:
    """RFC 3339 timestamp from ``kubectl logs --timestamps`` as integer nanoseconds.

    Compared numerically, not as strings: Kubernetes trims trailing zeros from
    the fraction, so string order is not time order.
    """
    from datetime import datetime, timezone

    m = _TS.match(ts or "")
    if not m:
        return None
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    frac = (m.group(2) or "").ljust(9, "0")
    return int(base.timestamp()) * 1_000_000_000 + int(frac)


def parse_log_output(text: str, cursor: int | None) -> tuple[list[str], int | None]:
    """Lines newer than ``cursor`` and the new cursor."""
    newest = cursor
    lines = []
    for raw_line in (text or "").splitlines():
        m = _LOG_LINE.match(raw_line)
        if not m:
            continue
        ts = parse_ts_ns(m.group("ts"))
        if ts is None or (cursor is not None and ts <= cursor):
            continue
        newest = ts if newest is None else max(newest, ts)
        lines.append(m.group("msg")[:MAX_LINE_CHARS])
    return lines, newest


def _log_priority(snapshot: dict[str, Any], open_subjects: set[str]) -> list[str]:
    """Workloads in the order their logs are worth reading."""
    failing = {p["workload"] for p in snapshot["pods"].values()
               if not p["ready"] or any(c["restarts"] for c in p["containers"])}
    serving = {b for s in snapshot["services"].values() for b in s["backends"]}

    def rank(key: str) -> tuple[int, str]:
        if key in open_subjects or key in failing:
            return (0, key)
        if key in serving:
            return (1, key)
        return (2, key)

    return sorted(snapshot["workloads"], key=rank)


def collect_logs(run: Runner, snapshot: dict[str, Any], cursors: dict[str, int], since_seconds: int,
                 open_subjects: set[str] | None = None, rotation: int = 0,
                 max_workloads: int = MAX_LOG_WORKLOADS) -> tuple[dict[str, list[str]], list[str]]:
    """New log lines per workload since each workload's cursor.

    At most ``max_workloads`` workloads are read per poll: the ones failing or
    under an open finding first, then those serving a Service, then the rest in
    rotation. A workload absent from the result was not read this poll, which
    the detector treats differently from "read, and logged nothing".
    """
    order = _log_priority(snapshot, open_subjects or set())
    if len(order) <= max_workloads:
        chosen = order
    else:
        head, rest = order[: max_workloads // 2], order[max_workloads // 2:]
        start = rotation % len(rest)
        chosen = head + (rest[start:] + rest[:start])[: max_workloads - len(head)]
    out: dict[str, list[str]] = {}
    blind: list[str] = []
    for key in chosen:
        w = snapshot["workloads"][key]
        selector = _selector_arg(w.get("selector") or {})
        if not selector or not _SAFE_NAMESPACE.match(w["namespace"] or ""):
            continue
        command = (f"logs -n {w['namespace']} -l {selector} --all-containers --prefix --timestamps "
                   f"--since={int(since_seconds)}s --tail={LOG_TAIL} --limit-bytes={LOG_LIMIT_BYTES} "
                   f"--max-log-requests=10 --ignore-errors")
        try:
            text = run(command, COMMAND_TIMEOUT)
        except CommandBlocked as exc:
            blind.append(f"logs: {exc}")
            break  # the same policy refuses every logs command
        except KubectlError as exc:
            blind.append(f"logs {key}: {str(exc)[:120]}")
            continue
        lines, cursors[key] = parse_log_output(text, cursors.get(key))
        out[key] = lines
    return out, blind

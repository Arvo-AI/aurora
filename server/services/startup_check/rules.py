"""Startup Check rules — incident preconditions, sourced from aurora-lab.

Each rule maps to a fault class Aurora's own lab (github.com/Arvo-AI/
aurora-lab, scenarios 01-21) has empirically shown becomes a production
incident. Two disciplines keep the check-engine light honest:

1. Layer 1 rules fire ONLY on provable in-repo inconsistencies (e.g. a
   Service selector that matches no workload anywhere in the repo). No
   speculation, no "best practice" nagging — those are a linter's job.
2. Anything the repo cannot know (node sizes, measured memory usage,
   which StorageClasses exist) is a Layer 2 rule that stays silent until
   the user provides the fact, and is suppressible via ``known_*`` lists.

Each rule gets a user-facing explanation that names the incident it
prevents, so the result reads as "here is the outage this repo is set
up for", not as lint output.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List

from services.startup_check.context import UserContext
from services.startup_check.model import (
    RepoIndex,
    container_ports,
    make_finding,
    parse_cpu,
    parse_mb,
    workload_ports,
)

_PROBE_KINDS = ("livenessProbe", "readinessProbe", "startupProbe")
_HOST_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*_HOST$")
_KNOWN_EXTERNAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


def _svc_dns_search(value: str) -> str | None:
    """Extract the Service name from a `*.svc[.cluster.local]` hostname.

    `redis.default.svc.cluster.local` -> "redis" (first label is the
    service, the second is the namespace — which our namespace
    approximation ignores in favor of the name)."""
    m = re.match(
        r"^(?P<svc>[a-z0-9]([a-z0-9-]*[a-z0-9])?)"
        r"(?:\.[a-z0-9-]+)*\.svc(?:\.|$)",
        value.strip(),
    )
    return m.group("svc") if m else None


# ---------------------------------------------------------------------------
# Layer 1 — shipped baseline (in-repo provable)
# ---------------------------------------------------------------------------


def svc_selector_matches_no_pods(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 07 / 15: a Service whose selector matches no workload in the repo
    routes to nothing — requests fail with 502/503 while every deploy looks
    green."""
    findings: List[Dict] = []
    for svc in index.services:
        selector = ((svc.manifest.get("spec") or {}).get("selector")) or {}
        if not selector:
            continue
        matches = index.workloads_for_service(svc)
        if matches:
            continue
        if any(w in ctx.known_workloads for w in selector.values()):
            continue
        findings.append(
            make_finding(
                rule="svc-selector-no-pods",
                severity="HIGH",
                file_path=svc.file_path,
                line=svc.line,
                title=f"Service '{svc.name}' selector matches no workload in this repo",
                explanation=(
                    f"Service '{svc.name}' selects {selector}, but no Deployment/StatefulSet/"
                    f"DaemonSet/Pod in the repo carries those labels. If deployed as-is, the "
                    f"Service has no endpoints and requests to it fail (502/503) while rollouts "
                    f"look healthy — aurora-lab scenario 07/15. If the matching workload lives "
                    f"in another repo or Helm chart, add its name to known_workloads to clear "
                    f"this code."
                ),
            )
        )
    return findings


def svc_targetport_undeclared(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 11: a Service targetPort that no selected container declares
    (numeric) or names (named) means kube-proxy forwards to a port nobody
    listens on — high error rate even though every pod is Ready."""
    findings: List[Dict] = []
    for svc in index.services:
        matches = index.workloads_for_service(svc)
        if not matches:
            continue  # selector rule already fired
        declared: List[Dict[str, Any]] = []
        for w in matches:
            declared.extend(workload_ports(w))
        for port_entry in (svc.manifest.get("spec") or {}).get("ports") or []:
            tp = port_entry.get("targetPort")
            if tp is None:
                continue  # defaults to port — valid
            ok = False
            for p in declared:
                if str(tp) == str(p.get("containerPort", "")):
                    ok = True
                if isinstance(tp, str) and tp == p.get("name"):
                    ok = True
            if ok:
                continue
            findings.append(
                make_finding(
                    rule="svc-targetport-mismatch",
                    severity="HIGH",
                    file_path=svc.file_path,
                    line=svc.line,
                    title=f"Service '{svc.name}' targetPort {tp} matches no declared container port",
                    explanation=(
                        f"Service '{svc.name}' forwards to targetPort {tp}, but the workloads it "
                        f"selects declare no such containerPort "
                        f"(declared: {sorted({str(p.get('containerPort') or p.get('name')) for p in declared})}). "
                        f"Traffic is forwarded to a closed port: high error rate while every pod "
                        f"shows Ready — aurora-lab scenario 11."
                    ),
                )
            )
    return findings


def probe_port_undeclared(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 06: a liveness/readiness probe on a port the container never
    declares (named port that does not exist, or numeric port not in the
    container's port list) — the probe fails and the pod CrashLoops or
    never goes Ready even though the app itself is fine."""
    findings: List[Dict] = []
    for w in index.workloads:
        for container in w.containers():
            declared = container_ports(container)
            for probe_kind in _PROBE_KINDS:
                probe = container.get(probe_kind)
                if not isinstance(probe, dict):
                    continue
                # The probed port sits on the probe itself (rare) or under
                # its action: httpGet / tcpSocket / grpc.
                port = probe.get("port")
                if port is None:
                    for action in ("httpGet", "tcpSocket", "grpc"):
                        handler = probe.get(action)
                        if isinstance(handler, dict) and handler.get("port") is not None:
                            port = handler.get("port")
                            break
                if port is None:
                    continue
                if isinstance(port, str):
                    named = any(p.get("name") == port for p in declared)
                    if not named:
                        findings.append(
                            make_finding(
                                rule="probe-port-undeclared",
                                severity="HIGH",
                                file_path=w.file_path,
                                line=w.line,
                                title=f"{probe_kind} on '{w.name}' references named port '{port}' that container '{container.get('name')}' does not declare",
                                explanation=(
                                    f"Container '{container.get('name')}' defines no port named "
                                    f"'{port}', so the {probe_kind} probes a port that does not "
                                    f"resolve. The pod restarts endlessly (CrashLoopBackOff) or "
                                    f"never becomes Ready even if the app is healthy — aurora-lab "
                                    f"scenario 06. Add the named containerPort or point the probe "
                                    f"at the real one."
                                ),
                            )
                        )
                else:
                    numeric = any(p.get("containerPort") == port for p in declared)
                    if not numeric:
                        findings.append(
                            make_finding(
                                rule="probe-port-undeclared",
                                severity="MEDIUM",
                                file_path=w.file_path,
                                line=w.line,
                                title=f"{probe_kind} on '{w.name}' probes port {port}, not in container '{container.get('name')}' declared ports",
                                explanation=(
                                    f"The {probe_kind} targets port {port} but the container "
                                    f"declares {sorted(p.get('containerPort') for p in declared)}. "
                                    f"Apps can listen on undeclared ports, so verify first — but "
                                    f"if this is a typo (aurora-lab scenario 06 probes the wrong "
                                    f"port) the pod will CrashLoop or never go Ready. Declaring "
                                    f"the probed containerPort removes the ambiguity."
                                ),
                            )
                        )
    return findings


def configmap_ref_missing(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 04: env/volume references to a ConfigMap that is not defined
    anywhere in the repo — the pod never starts (CreateContainerConfigError)
    or the app starts with empty config and misbehaves."""
    findings: List[Dict] = []
    cm_names = set(index.names(index.configmaps)) | set(ctx.known_configmaps)

    def ref_name(ref: Dict[str, Any]) -> str:
        return ref.get("name") or ""

    for w in index.workloads:
        referenced: List[str] = []
        for container in w.containers():
            for env in container.get("env") or []:
                ref = (env.get("valueFrom") or {}).get("configMapKeyRef") or {}
                if ref_name(ref):
                    referenced.append(ref_name(ref))
            for ref in container.get("envFrom") or []:
                cm = ref.get("configMapRef") or {}
                if ref_name(cm):
                    referenced.append(ref_name(cm))
        for vol in (w.pod_spec or {}).get("volumes") or []:
            cm = (vol.get("configMap") or {})
            if ref_name(cm):
                referenced.append(ref_name(cm))
        for name in dict.fromkeys(referenced):  # dedupe, keep order
            if name in cm_names:
                continue
            findings.append(
                make_finding(
                    rule="configmap-ref-missing",
                    severity="HIGH",
                    file_path=w.file_path,
                    line=w.line,
                    title=f"'{w.name}' references ConfigMap '{name}' not found in this repo",
                    explanation=(
                        f"No ConfigMap named '{name}' is defined in the repo and it is not in "
                        f"your known_configmaps list. On deploy the pod fails with "
                        f"CreateContainerConfigError (rollout stuck) or starts with missing "
                        f"config — aurora-lab scenario 04. If it is created out-of-band "
                        f"(kustomize, ops tooling), add it to known_configmaps to clear this code."
                    ),
                )
            )
    return findings


def secret_ref_missing(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 04 (secret variant): env/volume references to a Secret that is not
    in the repo. MEDIUM by design: secrets are usually created out-of-band,
    so absence from the repo is weaker evidence than for ConfigMaps."""
    findings: List[Dict] = []
    secret_names = set(index.names(index.secrets)) | set(ctx.known_secrets)

    def ref_name(ref: Dict[str, Any]) -> str:
        return ref.get("name") or ""

    for w in index.workloads:
        referenced: List[str] = []
        for container in w.containers():
            for env in container.get("env") or []:
                ref = (env.get("valueFrom") or {}).get("secretKeyRef") or {}
                if ref_name(ref):
                    referenced.append(ref_name(ref))
            for ref in container.get("envFrom") or []:
                sec = ref.get("secretRef") or {}
                if ref_name(sec):
                    referenced.append(ref_name(sec))
        for vol in (w.pod_spec or {}).get("volumes") or []:
            if ref_name(vol.get("secret") or {}):
                referenced.append(ref_name(vol.get("secret") or {}))
            if vol.get("secretName"):
                referenced.append(vol.get("secretName"))
        for name in dict.fromkeys(referenced):
            if name in secret_names:
                continue
            findings.append(
                make_finding(
                    rule="secret-ref-missing",
                    severity="MEDIUM",
                    file_path=w.file_path,
                    line=w.line,
                    title=f"'{w.name}' references Secret '{name}' not found in this repo",
                    explanation=(
                        f"No Secret named '{name}' is defined in the repo. Secrets are normally "
                        f"not committed, so this may be fine — but if it is missing at deploy "
                        f"time the rollout sticks (CreateContainerConfigError) or the app runs "
                        f"with empty credentials, the aurora-lab scenario 04 shape. If it is "
                        f"created out-of-band, add it to known_secrets to clear this code."
                    ),
                )
            )
    return findings


def env_host_unresolved(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 10: an env var pointing at an in-cluster hostname that matches no
    Service in the repo (REDIS_HOST=rediss, *.svc.cluster.local names) —
    DNS resolves to nothing and the pod CrashLoops on connection errors."""
    findings: List[Dict] = []
    known = set(index.names(index.services)) | set(ctx.known_services)
    for w in index.workloads:
        for container in w.containers():
            for env in container.get("env") or []:
                name = env.get("name") or ""
                value = env.get("value")
                if not isinstance(value, str) or not value.strip():
                    continue
                value = value.strip()
                if value in _KNOWN_EXTERNAL_HOSTS or "://" in value:
                    continue
                svc = _svc_dns_search(value)
                if svc is None:
                    # Narrow second case: *_HOST naming a bare in-cluster DNS label
                    if not _HOST_ENV_RE.match(name):
                        continue
                    if "." in value or not value or not value[0].isalpha():
                        continue
                    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", value):
                        continue
                    svc = value
                if svc in known:
                    continue
                # Suppress when a similarly-named Service exists (likely intentional)
                if any(svc in s for s in known):
                    continue
                findings.append(
                    make_finding(
                        rule="env-host-unresolved",
                        severity="MEDIUM",
                        file_path=w.file_path,
                        line=w.line,
                        title=f"Env var {name} points at '{value}' but no Service named '{svc}' exists in this repo",
                        explanation=(
                            f"{name}='{value}' looks like an in-cluster service hostname, but no "
                            f"Service named '{svc}' is defined in the repo (and it is not in "
                            f"known_services). If that name does not resolve in the target "
                            f"namespace, the pod CrashLoops on DNS/connection errors — aurora-lab "
                            f"scenario 10 (wrong REDIS_HOST). If the Service is created "
                            f"elsewhere, add it to known_services to clear this code."
                        ),
                    )
                )
    return findings


def sts_volumeclaim_mismatch(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 09-adjacent: a StatefulSet volumeMount naming a volume that has
    neither a matching volumeClaimTemplate nor a declared volume — the pod
    can never bind its storage and stays Pending/ContainerCreating."""
    findings: List[Dict] = []
    for w in index.workloads:
        if w.kind != "StatefulSet":
            continue
        spec = w.manifest.get("spec") or {}
        vct_names = {
            (v.get("metadata") or {}).get("name") for v in spec.get("volumeClaimTemplates") or []
        }
        vol_names = {
            v.get("name") for v in ((w.pod_spec or {}).get("volumes") or [])
        }
        for container in w.containers():
            for mount in container.get("volumeMounts") or []:
                mname = mount.get("name")
                if not mname:
                    continue
                if mname in vct_names or mname in vol_names:
                    continue
                findings.append(
                    make_finding(
                        rule="sts-volumeclaim-mismatch",
                        severity="HIGH",
                        file_path=w.file_path,
                        line=w.line,
                        title=(
                            f"StatefulSet '{w.name}' mounts volume '{mname}' that is neither a "
                            f"volumeClaimTemplate nor a declared volume"
                        ),
                        explanation=(
                            f"volumeMount '{mname}' in container '{container.get('name')}' has no "
                            f"matching volumeClaimTemplate ({sorted(v for v in vct_names if v)}) "
                            f"and no matching entry under spec.template.spec.volumes. The pod "
                            f"cannot be scheduled with its storage and stays Pending — the "
                            f"aurora-lab scenario 09 family (storage never binds)."
                        ),
                    )
                )
    return findings


def quota_blocks_surge(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 14: a ResourceQuota too small for a Deployment's surge pod —
    every rollout hangs mid-update because the surge pod is forbidden."""
    findings: List[Dict] = []
    for quota in index.quotas:
        hard = (quota.manifest.get("spec") or {}).get("hard") or {}
        quota_cpu = parse_cpu(hard.get("cpu"))
        quota_mem_mb = parse_mb(hard.get("memory"))
        if quota_cpu is None and quota_mem_mb is None:
            continue
        for w in index.workloads:
            if w.kind != "Deployment":
                continue
            # Explicit-namespace-only: cross-repo approximation would fire falsely
            if not (quota.namespace and w.namespace and quota.namespace == w.namespace):
                continue
            spec = w.manifest.get("spec") or {}
            replicas = spec.get("replicas") or 1
            strategy = spec.get("strategy") or {}
            surge = (strategy.get("rollingUpdate") or {}).get("maxSurge", "25%")
            if isinstance(surge, str) and surge.endswith("%"):
                surge_count = math.ceil(replicas * float(surge[:-1]) / 100)
            elif isinstance(surge, int):
                surge_count = surge
            else:
                surge_count = math.ceil(replicas * 0.25)
            peak_pods = replicas + surge_count
            pod_cpu = 0.0
            pod_mem_mb = 0.0
            for container in w.containers():
                req = container.get("resources") or {}
                pod_cpu += parse_cpu((req.get("requests") or {}).get("cpu")) or 0.0
                pod_mem_mb += parse_mb((req.get("requests") or {}).get("memory")) or 0.0
            messages = []
            if quota_cpu is not None and pod_cpu > 0 and peak_pods * pod_cpu > quota_cpu:
                messages.append(
                    f"peak rollout usage {peak_pods}x{pod_cpu}={peak_pods * pod_cpu:.2f} "
                    f"cores exceeds quota {hard.get('cpu')}"
                )
            if quota_mem_mb is not None and pod_mem_mb > 0 and peak_pods * pod_mem_mb > quota_mem_mb:
                messages.append(
                    f"peak rollout memory {peak_pods}x{pod_mem_mb:.0f}="
                    f"{peak_pods * pod_mem_mb:.0f}MB exceeds quota {hard.get('memory')}"
                )
            if not messages:
                continue
            findings.append(
                make_finding(
                    rule="quota-blocks-surge",
                    severity="MEDIUM",
                    file_path=w.file_path,
                    line=w.line,
                    title=f"ResourceQuota '{quota.name}' cannot fit '{w.name}' rollout surge",
                    explanation=(
                        f"During a rolling update, '{w.name}' briefly runs {replicas} + "
                        f"{surge_count} surge pod(s). " + "; ".join(messages) + ". The surge "
                        f"pod is forbidden and the rollout sticks mid-update — aurora-lab "
                        f"scenario 14. Quotas are checked against requests at pod creation, "
                        f"so raise the quota or lower the requests/maxSurge."
                    ),
                )
            )
    return findings


def image_uses_latest(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 03-adjacent: ':latest'/untagged workload images — a bad or "
    drifting tag deploys silently and rollback has nothing pinned."""
    findings: List[Dict] = []
    for w in index.workloads:
        for container in w.containers():
            image = container.get("image") or ""
            if not image:
                continue
            tag = image.rsplit("/", 1)[-1].split(":", 1)
            has_tag = ":" in image.rsplit("/", 1)[-1] and not image.rsplit("/", 1)[-1].endswith(":")
            if has_tag and tag[1] != "latest":
                continue
            findings.append(
                make_finding(
                    rule="image-uses-latest",
                    severity="LOW",
                    file_path=w.file_path,
                    line=w.line,
                    title=f"Container '{container.get('name')}' in '{w.name}' uses '{image}' (no pinned tag)",
                    explanation=(
                        f"Without a pinned tag, deploys pull whatever 'latest' is at that "
                        f"moment — an accidental or registry-drifted tag (aurora-lab scenario 03) "
                        f"ships silently and rollback has no digest to return to. Pin the tag "
                        f"or a digest."
                    ),
                )
            )
    return findings


def request_exceeds_limit(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """A container whose requests exceed its own limits — invalid sizing
    that can never be satisfied as written."""
    findings: List[Dict] = []
    for w in index.workloads:
        for container in w.containers():
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            for resource, parse in (("cpu", parse_cpu), ("memory", parse_mb)):
                req = parse(requests.get(resource))
                lim = parse(limits.get(resource))
                if req is None or lim is None:
                    continue
                if req > lim:
                    findings.append(
                        make_finding(
                            rule="request-exceeds-limit",
                            severity="MEDIUM",
                            file_path=w.file_path,
                            line=w.line,
                            title=(
                                f"Container '{container.get('name')}' in '{w.name}' requests more "
                                f"{resource} ({requests.get(resource)}) than its limit "
                                f"({limits.get(resource)})"
                            ),
                            explanation=(
                                f"requests.{resource} > limits.{resource} is a sizing error: the "
                                f"scheduler reserves more than the kernel will ever allow the "
                                f"container to use, so it is OOMKilled/CPU-throttled below its "
                                f"reservation (aurora-lab scenario 01/02 family). Fix one side."
                            ),
                        )
                    )
    return findings


# ---------------------------------------------------------------------------
# Layer 2 — user context (only fires when the user provided the fact)
# ---------------------------------------------------------------------------


def request_exceeds_node(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 08: requests larger than every node the user has — the pod can
    never schedule and stays Pending with FailedScheduling."""
    max_cpu = ctx.max_node_cpu_cores()
    max_mem_mb = ctx.max_node_memory_mb()
    if max_cpu is None and max_mem_mb is None:
        return []
    findings: List[Dict] = []
    for w in index.workloads:
        for container in w.containers():
            requests = (container.get("resources") or {}).get("requests") or {}
            req_cpu = parse_cpu(requests.get("cpu"))
            req_mem_mb = parse_mb(requests.get("memory"))
            problems = []
            if req_cpu is not None and max_cpu is not None and req_cpu > max_cpu:
                problems.append(f"cpu request {requests['cpu']} > largest node ({max_cpu} cores)")
            if req_mem_mb is not None and max_mem_mb is not None and req_mem_mb > max_mem_mb:
                problems.append(
                    f"memory request {requests['memory']} > largest node ({max_mem_mb:.0f}MB)"
                )
            if not problems:
                continue
            findings.append(
                make_finding(
                    rule="request-exceeds-node",
                    severity="HIGH",
                    source="context",
                    file_path=w.file_path,
                    line=w.line,
                    title=f"Container '{container.get('name')}' in '{w.name}' cannot fit on any of your nodes",
                    explanation=(
                        "; ".join(problems)
                        + ". The pod stays Pending (FailedScheduling/Insufficient cpu) and "
                        "blocks its rollout — aurora-lab scenario 08. Per your node list, no "
                        "node can ever accept this request."
                    ),
                )
            )
    return findings


def limit_below_measured_usage(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 01 / 02: a memory limit below the usage you measured — the kernel
    OOMKills the container (exit 137) even though nothing changed."""
    if not ctx.service_memory_mb:
        return []
    findings: List[Dict] = []
    for w in index.workloads:
        usage_mb = ctx.service_memory_mb.get(w.pod_name) or ctx.service_memory_mb.get(w.name)
        if usage_mb is None:
            continue
        for container in w.containers():
            limits = (container.get("resources") or {}).get("limits") or {}
            limit_mb = parse_mb(limits.get("memory"))
            if limit_mb is None:
                continue
            if limit_mb < usage_mb:
                findings.append(
                    make_finding(
                        rule="limit-below-usage",
                        severity="HIGH",
                        source="context",
                        file_path=w.file_path,
                        line=w.line,
                        title=f"'{w.pod_name}' memory limit ({limits.get('memory')}) is below your measured usage ({usage_mb:.0f}MB)",
                        explanation=(
                            f"You reported '{w.pod_name}' uses ~{usage_mb:.0f}MB, but its "
                            f"memory limit is {limits.get('memory')} "
                            f"({limit_mb:.0f}MB). The kernel OOMKills it at the limit "
                            f"(exit 137, OOMKilled) and the pod CrashLoops — aurora-lab "
                            f"scenarios 01/02. Raise the limit above measured usage plus "
                            f"headroom, or bound the workload's memory."
                        ),
                    )
                )
    return findings


def pvc_unknown_storageclass(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 09: a PVC whose storageClassName does not exist (or no
    storageClassName with no default) — the claim sits Pending forever and
    the symptom shows up in an unrelated-looking pod."""
    if not ctx.storage_classes and ctx.default_storage_class is None:
        return []
    known = set(ctx.storage_classes)
    findings: List[Dict] = []
    for pvc in index.pvcs:
        spec = pvc.manifest.get("spec") or {}
        sc = spec.get("storageClassName")
        if sc:
            if known and sc not in known:
                findings.append(
                    make_finding(
                        rule="pvc-unknown-storageclass",
                        severity="HIGH",
                        source="context",
                        file_path=pvc.file_path,
                        line=pvc.line,
                        title=f"PVC '{pvc.name}' references StorageClass '{sc}' that is not in your cluster",
                        explanation=(
                            f"You listed your StorageClasses as {sorted(known)}; '{sc}' is not "
                            f"one of them. The claim stays Pending (no StorageClass matches) and "
                            f"every pod mounting it waits forever — aurora-lab scenario 09, "
                            f"where the symptom surfaced as an unrelated pod CrashLooping."
                        ),
                    )
                )
        elif ctx.default_storage_class is False:
            findings.append(
                make_finding(
                    rule="pvc-unknown-storageclass",
                    severity="HIGH",
                    source="context",
                    file_path=pvc.file_path,
                    line=pvc.line,
                    title=f"PVC '{pvc.name}' has no storageClassName and your cluster has no default StorageClass",
                    explanation=(
                        "With no storageClassName and no default StorageClass, this claim is "
                        "never bound — the pod waits in Pending/ContainerCreating indefinitely "
                        "(aurora-lab scenario 09). Set a valid storageClassName or enable "
                        "DefaultStorageClass admission."
                    ),
                )
            )
    return findings


def dns_wrong_search_domain(index: RepoIndex, ctx: UserContext) -> List[Dict]:
    """Lab 16: dnsConfig search domains for a different cluster domain —
    short names resolve to the wrong (or nonexistent) cluster and the pod
    CrashLoops on DNS errors."""
    if not ctx.cluster_domain:
        return []
    expected_suffix = f".svc.{ctx.cluster_domain}"
    findings: List[Dict] = []
    for w in index.workloads:
        dns = (w.pod_spec or {}).get("dnsConfig") or {}
        searches = dns.get("searches") or []
        for search in searches:
            if ".svc." not in search:
                continue
            if not search.endswith(expected_suffix):
                findings.append(
                    make_finding(
                        rule="dns-wrong-search-domain",
                        severity="MEDIUM",
                        source="context",
                        file_path=w.file_path,
                        line=w.line,
                        title=f"'{w.name}' dnsConfig search domain '{search}' does not match your cluster domain '{ctx.cluster_domain}'",
                        explanation=(
                            f"Your cluster domain is '{ctx.cluster_domain}', so in-cluster names "
                            f"resolve under '{expected_suffix}'. '{search}' belongs to a "
                            f"different domain: short hostnames resolve to nothing and the pod "
                            f"CrashLoops on DNS failures — aurora-lab scenario 16."
                        ),
                    )
                )
    return findings


# Rule registry: order is stable; ids appear in findings and dismissal lists.
BASELINE_RULES = [
    svc_selector_matches_no_pods,
    svc_targetport_undeclared,
    probe_port_undeclared,
    configmap_ref_missing,
    secret_ref_missing,
    env_host_unresolved,
    sts_volumeclaim_mismatch,
    quota_blocks_surge,
    request_exceeds_limit,
    image_uses_latest,
]

CONTEXT_RULES = [
    request_exceeds_node,
    limit_below_measured_usage,
    pvc_unknown_storageclass,
    dns_wrong_search_domain,
]


def run_all_rules(index: RepoIndex, ctx: UserContext) -> List[Dict[str, Any]]:
    """Run every rule, collect findings, stable order, dedupe by id."""
    findings: List[Dict[str, Any]] = []
    seen = set()
    for rule in BASELINE_RULES + CONTEXT_RULES:
        try:
            for finding in rule(index, ctx):
                if finding["id"] in seen:
                    continue
                seen.add(finding["id"])
                findings.append(finding)
        except Exception:  # a broken rule must not kill the scan
            import logging

            logging.getLogger(__name__).exception(
                "startup check rule %s failed", getattr(rule, "__name__", "?")
            )
    return findings

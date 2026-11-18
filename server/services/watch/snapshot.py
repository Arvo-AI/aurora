"""A compact, JSON-serialisable model of a cluster, built from ``kubectl get -o json``.

The detector compares two of these at a time, so a snapshot keeps only what a
regression or its cause can show up in: who should be running and who is, what
each Service routes to, which Warning events fired, and a fingerprint of every
object whose change could explain a regression.

Nothing secret is kept. Secrets are never listed (only *references* to them in
pod templates are), ConfigMap values are reduced to a hash per key, and env
values whose name looks like a credential are replaced with ``<redacted>``.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime
from typing import Any

# Waiting reasons that mean a container cannot start, as opposed to "is starting".
BAD_WAITING = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
    "ErrImageNeverPull", "PreCreateHookError", "PostStartHookError",
})

_SENSITIVE_NAME = re.compile(r"pass|secret|token|key|credential|auth|private|cert|dsn|session", re.I)
# A URL's password, the user possibly empty (redis://:pw@host). It runs to the last
# "@", so a password containing "/" or "@" is masked too (over-masking is fine). The
# scheme is bounded so a long run of word characters cannot make it slow.
_URL_PASSWORD = re.compile(r"(\b[A-Za-z][\w+.-]{0,30}://[^:/@\s]*):(\S*)@")
# A flag whose name ends in a credential word, and its value, joined or not:
# --requirepass x, --db-password=x, --account-key x, -dev-root-token-id x,
# -Dapi.key=x. Not --token-ttl, --keyspace, --max-keys or --password-file.
_FLAG = r"--?[\w.-]*?(?:password|passwd|passphrase|pass|pwd|secret|token(?:[-_]?id)?|key|credentials?|auth)"
_SECRET_FLAG = re.compile(rf"^{_FLAG}$", re.I)
_SECRET_FLAG_VALUE = re.compile(rf"(?<![\w-])({_FLAG})(=|\s+)(?!<redacted|-)(\S+)", re.I)
# Query parameters, matched in linear time; a credential-named one is masked:
# /-/ready?token=x, ?access_token=x, &sig=x.
_QUERY_PARAM = re.compile(r"([?&])([^=&#\s?]{1,200})=([^&#\s]+)")
_CREDENTIAL_WORD = re.compile(r"token|key|pass|secret|auth|sig", re.I)
# Masked values keep a digest keyed by a server secret, so a rotated password still
# shows as a change while neither the value nor a hash that could be checked against
# guesses is stored. Without the secret (some dev installs) a masked value is just
# "<redacted>", and a rotation does not show.
_DIGEST_KEY = os.getenv("INTERNAL_API_SECRET", "").encode() or None
# Which key made the digests: a state made with another key (the secret was rotated)
# would see every masked value as changed, so it is learned again instead.
DIGEST_ID = (hmac.new(_DIGEST_KEY, b"aurora-watch digest key id", hashlib.sha256).hexdigest()[:8]
             if _DIGEST_KEY else "none")


def _digest(value: str) -> str:
    """``#`` and 8 hex chars of a keyed digest, or "" without a key."""
    if _DIGEST_KEY is None:
        return ""
    return "#" + hmac.new(_DIGEST_KEY, value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:8]


def _masked(value: str) -> str:
    digest = _digest(value)
    return f"<redacted {digest}>" if digest else "<redacted>"


def _mask_query(match: re.Match, tag: Any) -> str:
    sep, name, value = match.groups()
    return f"{sep}{name}={tag(value)}" if _CREDENTIAL_WORD.search(name) else match.group(0)


@functools.lru_cache(maxsize=20_000)  # pod specs barely change between polls
def _clean(value: str, digest: bool = True) -> str:
    """A literal from the cluster (a spec value, an event message), safe to store and
    show: NUL removed (jsonb cannot hold it), URL passwords, credential flag values and
    credential query parameters masked, other credentials redacted. With ``digest``, a
    masked or redacted value carries a keyed digest, so changing it is still a change."""
    from utils.security.output_redaction import redact

    tag = _masked if digest else (lambda _: "<redacted>")
    value = value.replace("\x00", "")
    masked = _URL_PASSWORD.sub(lambda m: f"{m.group(1)}:{tag(m.group(2))}@", value)
    masked = _SECRET_FLAG_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{tag(m.group(3))}", masked)
    masked = _QUERY_PARAM.sub(lambda m: _mask_query(m, tag), masked)
    out, findings = redact(masked)
    return f"{out} {_digest(value)}".rstrip() if findings and digest else out


def _argv(values: Any) -> list[str] | None:
    """A container's command or args, cleaned; the element after a credential-named
    flag (["--requirepass", "<pw>"]) is masked as well."""
    out: list[str] = []
    mask_next = False
    for value in values or []:
        value = str(value)[:500]
        out.append(_masked(value) if mask_next and not value.startswith("-") else _clean(value))
        mask_next = bool(_SECRET_FLAG.match(value))
    return out or None


def _message(value: Any, default: str = "") -> str:
    """A message the cluster wrote (an event, a waiting reason, a condition), cleaned."""
    return _clean(str(value or default)[:MAX_MESSAGE_CHARS], digest=False)

# Kinds tracked as possible causes, besides workloads and Services.
# kubectl resource name -> Kind.
TRACKED_OBJECTS = {
    "configmaps": "ConfigMap",
    "networkpolicies": "NetworkPolicy",
    "resourcequotas": "ResourceQuota",
    "limitranges": "LimitRange",
    "persistentvolumeclaims": "PersistentVolumeClaim",
    "horizontalpodautoscalers": "HorizontalPodAutoscaler",
    "poddisruptionbudgets": "PodDisruptionBudget",
}

MAX_MESSAGE_CHARS = 300

# Deployment Progressing reasons while a rollout is replacing pods
# ("NewReplicaSetAvailable" means it finished, "ProgressDeadlineExceeded" that it stalled).
_ROLLING_REASONS = frozenset({"ReplicaSetUpdated", "FoundNewReplicaSet", "NewReplicaSetCreated"})


def _pods_of(value: Any, total: int, round_up: bool) -> int | None:
    """An int-or-percent rollout bound (``maxUnavailable: 25%``) as a number of pods."""
    if isinstance(value, str) and value.endswith("%"):
        try:
            share = total * float(value[:-1]) / 100
        except ValueError:
            return None
        return int(math.ceil(share) if round_up else math.floor(share))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def rollout_strategy(kind: str, spec: dict[str, Any], desired: int, updated: int) -> tuple[bool, int]:
    """(held, max_unavailable) for a workload, with Kubernetes' defaults and rounding.

    ``held``: the rollout waits on purpose, so it is never "stuck": a paused
    Deployment, an OnDelete StatefulSet or DaemonSet (someone deletes the pods), or a
    StatefulSet whose partition keeps the rest of its pods on the old revision. It is
    still a rollout while it replaces pods. ``max_unavailable``: how many pods a
    rollout may take down at once; fewer ready than that is not the rollout's doing.
    """
    if kind == "Deployment":
        strategy = spec.get("strategy") or {}
        if strategy.get("type") == "Recreate":
            return bool(spec.get("paused")), desired
        rolling = strategy.get("rollingUpdate") or {}
        surge = _pods_of(rolling.get("maxSurge", "25%"), desired, True) or 0
        unavailable = _pods_of(rolling.get("maxUnavailable", "25%"), desired, False) or 0
        if surge == 0 and unavailable == 0:
            unavailable = 1  # what the Deployment controller does when both round down to zero
        return bool(spec.get("paused")), unavailable
    strategy = spec.get("updateStrategy") or {}
    if strategy.get("type") == "OnDelete":
        return True, 1  # pods replaced by hand, conventionally one at a time
    rolling = strategy.get("rollingUpdate") or {}
    if kind == "StatefulSet":
        partition = _pods_of(rolling.get("partition"), desired, False) or 0
        return (partition > 0 and updated >= desired - partition,
                max(1, _pods_of(rolling.get("maxUnavailable", 1), desired, False) or 1))
    return False, max(1, _pods_of(rolling.get("maxUnavailable", 1), desired, True) or 1)


def parse_ts(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def stable_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _last_write(meta: dict[str, Any]) -> float | None:
    """Most recent write recorded on the object (managedFields), else its creation time."""
    times = [parse_ts(f.get("time")) for f in meta.get("managedFields") or [] if isinstance(f, dict)]
    times = [t for t in times if t is not None]
    return max(times) if times else parse_ts(meta.get("creationTimestamp"))


def _env_value(entry: dict[str, Any]) -> str:
    name = entry.get("name") or ""
    if "value" in entry:
        raw = str(entry.get("value"))
        if _SENSITIVE_NAME.search(name):
            return _masked(raw)
        value = _clean(raw[:1000])
        if len(value) > 120:
            # Cut for length: a digest of the whole value keeps an edit past the cut (or a
            # masked value's digest that the cut removed) visible as a change.
            value = f"{value[:117]}... {_digest(raw)}".rstrip()
        return value
    ref = entry.get("valueFrom") or {}
    for kind, label in (("secretKeyRef", "secret"), ("configMapKeyRef", "configmap")):
        if kind in ref:
            r = ref.get(kind) or {}
            return f"<from {label} {r.get('name')}/{r.get('key')}>"
    if "fieldRef" in ref:
        return f"<field {(ref.get('fieldRef') or {}).get('fieldPath')}>"
    if "resourceFieldRef" in ref:
        return f"<resource {(ref.get('resourceFieldRef') or {}).get('resource')}>"
    return "<ref>"


def _probe(probe: Any) -> Any:
    """A probe with its credentials removed: header values, query credentials in the
    path, and exec arguments (masked the way container args are)."""
    if not isinstance(probe, dict):
        return probe
    out = json.loads(json.dumps(probe))
    http = out.get("httpGet") if isinstance(out.get("httpGet"), dict) else {}
    for header in http.get("httpHeaders") or []:
        if isinstance(header, dict) and "value" in header:
            header["value"] = _masked(str(header["value"]))
    for field in ("path", "host"):  # /-/ready?token=...
        if isinstance(http.get(field), str):
            http[field] = _clean(http[field][:500])
    if isinstance(out.get("exec"), dict):
        out["exec"]["command"] = _argv(out["exec"].get("command")) or []
    return out


def _container_summary(c: dict[str, Any]) -> dict[str, Any]:
    env_from = []
    for e in c.get("envFrom") or []:
        if not isinstance(e, dict):
            continue
        label = "secret" if "secretRef" in e else "configmap"
        env_from.append(f"{label} {(e.get('secretRef') or e.get('configMapRef') or {}).get('name')}")
    return {
        "image": c.get("image"),
        "env": {e.get("name"): _env_value(e) for e in c.get("env") or [] if isinstance(e, dict)},
        "envFrom": sorted(env_from),
        "resources": c.get("resources") or {},
        "command": _argv(c.get("command")),
        "args": _argv(c.get("args")),
        "ports": [[p.get("containerPort"), p.get("protocol", "TCP")] for p in c.get("ports") or []],
        "livenessProbe": _probe(c.get("livenessProbe")),
        "readinessProbe": _probe(c.get("readinessProbe")),
        "startupProbe": _probe(c.get("startupProbe")),
    }


def _volume_summary(v: dict[str, Any]) -> str:
    for key, label, field in (
        ("configMap", "configmap", "name"),
        ("secret", "secret", "secretName"),
        ("persistentVolumeClaim", "pvc", "claimName"),
    ):
        if key in v:
            return f"{label} {(v.get(key) or {}).get(field)}"
    for key in ("emptyDir", "hostPath", "projected", "downwardAPI"):
        if key in v:
            return key
    return "other"


def summarize_template(template: dict[str, Any]) -> dict[str, Any]:
    spec = template.get("spec") or {}
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    return {
        "containers": {c.get("name"): _container_summary(c) for c in spec.get("containers") or []},
        "initContainers": {c.get("name"): _container_summary(c) for c in spec.get("initContainers") or []},
        "volumes": {v.get("name"): _volume_summary(v) for v in spec.get("volumes") or []},
        "dnsPolicy": spec.get("dnsPolicy"),
        "dnsConfig": spec.get("dnsConfig"),
        "nodeSelector": spec.get("nodeSelector"),
        "serviceAccountName": spec.get("serviceAccountName"),
        "restartedAt": annotations.get("kubectl.kubernetes.io/restartedAt"),
    }


_KIND_OF_LABEL = {"configmap": "ConfigMap", "secret": "Secret", "pvc": "PersistentVolumeClaim"}


def template_references(summary: dict[str, Any]) -> dict[str, list[str]]:
    """ConfigMaps, Secrets and PVCs a pod template depends on, by Kind."""
    refs: dict[str, set[str]] = {kind: set() for kind in _KIND_OF_LABEL.values()}
    for vol in (summary.get("volumes") or {}).values():
        label, _, name = vol.partition(" ")
        if label in _KIND_OF_LABEL and name:
            refs[_KIND_OF_LABEL[label]].add(name)
    for group in ("containers", "initContainers"):
        for c in (summary.get(group) or {}).values():
            for value in (c.get("env") or {}).values():
                m = re.match(r"<from (secret|configmap) ([^/>]+)/", value or "")
                if m:
                    refs[_KIND_OF_LABEL[m.group(1)]].add(m.group(2))
            for item in c.get("envFrom") or []:
                label, _, name = item.partition(" ")
                if label in _KIND_OF_LABEL and name:
                    refs[_KIND_OF_LABEL[label]].add(name)
    return {kind: sorted(names) for kind, names in refs.items() if names}


def _owner_workload(pod: dict[str, Any], rs_owner: dict[str, str]) -> str:
    meta = pod.get("metadata") or {}
    ns = meta.get("namespace")
    for ref in meta.get("ownerReferences") or []:
        kind, name = ref.get("kind"), ref.get("name")
        if kind == "ReplicaSet":
            return rs_owner.get(f"{ns}/{name}") or f"ReplicaSet/{ns}/{name}"
        if kind in ("StatefulSet", "DaemonSet", "Job"):
            return f"{kind}/{ns}/{name}"
    return f"Pod/{ns}/{meta.get('name')}"


def _container_states(statuses: list[dict[str, Any]] | None, init: bool) -> list[dict[str, Any]]:
    out = []
    for s in statuses or []:
        state = s.get("state") or {}
        last = (s.get("lastState") or {}).get("terminated") or {}
        waiting = state.get("waiting") or {}
        out.append({
            "name": s.get("name"),
            "init": init,
            "ready": bool(s.get("ready")),
            "running": "running" in state,
            "restarts": int(s.get("restartCount") or 0),
            "waiting": waiting.get("reason"),
            "waiting_message": _message(waiting.get("message")) or None,
            "last_reason": last.get("reason"),
            "last_exit_code": last.get("exitCode"),
        })
    return out


def _object_detail(kind: str, obj: dict[str, Any]) -> Any:
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    if kind == "PersistentVolumeClaim":
        return {"storageClassName": spec.get("storageClassName"), "phase": status.get("phase")}
    if kind == "ResourceQuota":
        return {"hard": spec.get("hard")}
    if kind == "NetworkPolicy":
        return {
            "podSelector": (spec.get("podSelector") or {}).get("matchLabels") or {},
            "policyTypes": spec.get("policyTypes"),
        }
    if kind == "HorizontalPodAutoscaler":
        return {"min": spec.get("minReplicas"), "max": spec.get("maxReplicas")}
    return None


def build_snapshot(raw: dict[str, Any], taken_at: float) -> dict[str, Any]:
    """Build a snapshot from parsed kubectl lists.

    ``raw`` maps a kubectl resource name (``pods``, ``deployments``,
    ``endpointslices`` or ``endpoints``...) to a ``{"items": [...]}`` list.
    Missing resource names are treated as empty (a blind spot, not an error).
    """

    def items(resource: str) -> list[dict[str, Any]]:
        value = raw.get(resource) or {}
        return [i for i in value.get("items") or [] if isinstance(i, dict)]

    rs_owner: dict[str, str] = {}
    for rs in items("replicasets"):
        meta = rs.get("metadata") or {}
        for ref in meta.get("ownerReferences") or []:
            if ref.get("kind") == "Deployment":
                rs_owner[f"{meta.get('namespace')}/{meta.get('name')}"] = (
                    f"Deployment/{meta.get('namespace')}/{ref.get('name')}"
                )

    workloads: dict[str, Any] = {}
    for kind, resource in (("Deployment", "deployments"), ("StatefulSet", "statefulsets"), ("DaemonSet", "daemonsets")):
        for obj in items(resource):
            meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
            key = f"{kind}/{meta.get('namespace')}/{meta.get('name')}"
            template = spec.get("template") or {}
            summary = summarize_template(template)
            if kind == "DaemonSet":
                desired = status.get("desiredNumberScheduled") or 0
                ready = status.get("numberReady") or 0
                updated = status.get("updatedNumberScheduled") or 0
            else:
                desired = 1 if spec.get("replicas") is None else spec.get("replicas")
                ready = status.get("readyReplicas") or 0
                updated = status.get("updatedReplicas") or 0
            conditions = {
                c.get("type"): {"status": c.get("status"), "reason": c.get("reason"),
                                "message": _message(c.get("message"))}
                for c in status.get("conditions") or []
            }
            # Kubernetes' own word that a rollout is under way (pods are being
            # replaced on purpose, so fewer may be ready for a while).
            if kind == "Deployment":
                reason = (conditions.get("Progressing") or {}).get("reason")
                converging = updated < desired or reason in _ROLLING_REASONS
            elif kind == "StatefulSet":
                converging = (updated < desired
                              or (status.get("updateRevision") or "") != (status.get("currentRevision") or ""))
            else:
                converging = updated < desired
            held, max_unavailable = rollout_strategy(kind, spec, int(desired), int(updated))
            workloads[key] = {
                "kind": kind, "namespace": meta.get("namespace"), "name": meta.get("name"),
                "desired": int(desired), "ready": int(ready), "updated": int(updated),
                "generation": meta.get("generation"), "observed_generation": status.get("observedGeneration"),
                "template_hash": stable_hash(summary), "template": summary,
                "references": template_references(summary),
                "selector": (spec.get("selector") or {}).get("matchLabels") or {},
                "pod_labels": (template.get("metadata") or {}).get("labels") or {},
                "conditions": conditions,
                "converging": bool(converging) and not held,
                "held": held,
                "max_unavailable": max_unavailable,
                # How long Kubernetes itself lets this rollout go without progress.
                "progress_deadline": spec.get("progressDeadlineSeconds") if kind == "Deployment" else None,
                "last_write": _last_write(meta),
            }

    pods: dict[str, Any] = {}
    for pod in items("pods"):
        meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
        if status.get("phase") == "Succeeded":
            continue
        labels = meta.get("labels") or {}
        conditions = {c.get("type"): c for c in status.get("conditions") or []}
        scheduled = conditions.get("PodScheduled") or {}
        unschedulable = None
        if scheduled.get("reason") == "Unschedulable":
            unschedulable = _message(scheduled.get("message"), "Unschedulable")
        pods[f"{meta.get('namespace')}/{meta.get('name')}"] = {
            "namespace": meta.get("namespace"), "name": meta.get("name"),
            "workload": _owner_workload(pod, rs_owner),
            "template_hash": labels.get("pod-template-hash") or labels.get("controller-revision-hash"),
            "labels": labels,
            "phase": status.get("phase"),
            "ready": (conditions.get("Ready") or {}).get("status") == "True",
            "created": parse_ts(meta.get("creationTimestamp")),
            "deleting": bool(meta.get("deletionTimestamp")),
            "unschedulable": unschedulable,
            "containers": _container_states(status.get("containerStatuses"), False)
            + _container_states(status.get("initContainerStatuses"), True),
        }

    ready_by_service: dict[str, int] = {}
    backends_by_service: dict[str, set[str]] = {}

    def _add_backend(key: str, namespace: str | None, target: dict[str, Any]) -> None:
        if target.get("kind") == "Pod":
            pod = pods.get(f"{target.get('namespace') or namespace}/{target.get('name')}")
            if pod:
                backends_by_service.setdefault(key, set()).add(pod["workload"])

    slices = items("endpointslices")
    for es in slices:
        meta = es.get("metadata") or {}
        svc = (meta.get("labels") or {}).get("kubernetes.io/service-name")
        if not svc:
            continue
        key = f"{meta.get('namespace')}/{svc}"
        for ep in es.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready") is not False:
                ready_by_service[key] = ready_by_service.get(key, 0) + 1
            _add_backend(key, meta.get("namespace"), ep.get("targetRef") or {})
    if not slices:
        # v1 Endpoints: the kubectl-agent's default RBAC grants these but not
        # discovery.k8s.io EndpointSlices.
        for ep in items("endpoints"):
            meta = ep.get("metadata") or {}
            key = f"{meta.get('namespace')}/{meta.get('name')}"
            for subset in ep.get("subsets") or []:
                for addr in subset.get("addresses") or []:
                    ready_by_service[key] = ready_by_service.get(key, 0) + 1
                    _add_backend(key, meta.get("namespace"), addr.get("targetRef") or {})

    services: dict[str, Any] = {}
    for svc in items("services"):
        meta, spec = svc.get("metadata") or {}, svc.get("spec") or {}
        key = f"{meta.get('namespace')}/{meta.get('name')}"
        ports = sorted(
            [str(p.get("port")), str(p.get("targetPort")), p.get("protocol", "TCP")]
            for p in spec.get("ports") or []
        )
        selector = spec.get("selector") or {}
        services[key] = {
            "namespace": meta.get("namespace"), "name": meta.get("name"),
            "selector": selector, "ports": ports, "type": spec.get("type"),
            "spec_hash": stable_hash({"selector": selector, "ports": ports, "type": spec.get("type")}),
            "ready_endpoints": ready_by_service.get(key, 0),
            "backends": sorted(backends_by_service.get(key, set())),
            "last_write": _last_write(meta),
        }

    events = []
    for ev in items("events"):
        meta = ev.get("metadata") or {}
        involved = ev.get("involvedObject") or ev.get("regarding") or {}
        last = (parse_ts(ev.get("lastTimestamp")) or parse_ts(ev.get("eventTime"))
                or parse_ts(meta.get("creationTimestamp")))
        events.append({
            "type": ev.get("type"), "reason": ev.get("reason"),
            "namespace": involved.get("namespace") or meta.get("namespace"),
            "kind": involved.get("kind"), "name": involved.get("name"),
            "message": _message(ev.get("message") or ev.get("note")),
            "count": int(ev.get("count") or (ev.get("series") or {}).get("count") or 1),
            "last": last,
        })

    objects: dict[str, Any] = {}
    for resource, kind in TRACKED_OBJECTS.items():
        for obj in items(resource):
            meta = obj.get("metadata") or {}
            if kind == "ConfigMap" and meta.get("name") == "kube-root-ca.crt":
                continue
            if kind == "ConfigMap":
                data = {**(obj.get("data") or {}), **(obj.get("binaryData") or {})}
                content: Any = {k: stable_hash(v) for k, v in data.items()}
                detail: Any = {"keys": len(data), "first": sorted(data.keys())[:10]}  # for "created"
            else:
                content = stable_hash(obj.get("spec") or {})
                detail = _object_detail(kind, obj)
            objects[f"{kind}/{meta.get('namespace')}/{meta.get('name')}"] = {
                "kind": kind, "namespace": meta.get("namespace"), "name": meta.get("name"),
                "content": content, "detail": detail, "last_write": _last_write(meta),
            }

    return {
        "taken_at": taken_at, "workloads": workloads, "pods": pods, "services": services,
        "events": events, "objects": objects,
        # Kinds this poll could not read: the detector carries their last state forward.
        "blind": sorted(raw.get("_blind") or []),
    }

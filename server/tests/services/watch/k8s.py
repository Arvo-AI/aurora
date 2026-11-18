"""A tiny in-memory cluster that renders the JSON ``kubectl get -o json`` returns.

Tests mutate it the way a real fault would (change an env var or a Service
selector, make a container crash-loop, stall a rollout) and render the next
poll, so the whole pipeline runs on realistic shapes: snapshot -> changes ->
detector. Like ``kubectl get``, objects carry no managedFields.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

NS = "shop"
T0 = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()[:9]


class Cluster:
    def __init__(self, now: float = T0) -> None:
        self.now = now
        self.deployments: dict[str, dict[str, Any]] = {}
        self.services: dict[str, dict[str, Any]] = {}
        self.configmaps: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, list[dict[str, Any]]] = {"networkpolicies": [], "resourcequotas": []}
        self.events: list[dict[str, Any]] = []

    # -- building ------------------------------------------------------------
    def deployment(self, name: str, replicas: int = 1, env: dict[str, str] | None = None,
                   configmap: str | None = None, strategy: dict[str, Any] | None = None) -> None:
        labels = {"app": name}
        container = {"name": name, "image": "python:3.12-alpine",
                     "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
                     "resources": {"limits": {"memory": "192Mi"}}}
        volumes = [{"name": "code", "configMap": {"name": configmap}}] if configmap else []
        self.deployments[name] = {
            "replicas": replicas, "labels": labels, "generation": 1, "created": self.now - 86_400,
            "template": {"metadata": {"labels": dict(labels)},
                         "spec": {"containers": [container], "volumes": volumes}},
            "updated": None, "fault": None, "restarts": 0, "replaced": 0, "unready": 0, "rolling": False,
            # roll() takes one pod out of service at a time, as maxUnavailable: 1 allows.
            "strategy": strategy or {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
            "paused": False,
        }

    def service(self, name: str, selector: dict[str, str], port: int = 80, target: int = 80) -> None:
        self.services[name] = {"selector": dict(selector), "port": port, "target": target}

    def configmap(self, name: str, data: dict[str, str]) -> None:
        self.configmaps[name] = {"data": dict(data)}

    # -- faults --------------------------------------------------------------
    def set_env(self, deployment: str, key: str, value: str) -> None:
        d = self.deployments[deployment]
        env = d["template"]["spec"]["containers"][0]["env"]
        for e in env:
            if e["name"] == key:
                e["value"] = value
                break
        else:
            env.append({"name": key, "value": value})
        d["generation"] += 1

    def set_selector(self, service: str, selector: dict[str, str]) -> None:
        self.services[service]["selector"] = dict(selector)

    def crash(self, deployment: str, reason: str = "OOMKilled", exit_code: int = 137,
              waiting: str = "CrashLoopBackOff") -> None:
        """Pods of the deployment's current revision crash-loop; each tick adds a restart."""
        self.deployments[deployment]["fault"] = {"waiting": waiting, "reason": reason, "exit_code": exit_code}

    def heal(self, deployment: str) -> None:
        self.deployments[deployment]["fault"] = None

    def stall(self, deployment: str, updated: int) -> None:
        """The deployment's rollout stops with ``updated`` pods on the new revision."""
        d = self.deployments[deployment]
        d["generation"] += 1
        d["updated"] = updated

    def roll(self, deployment: str, unready: int = 1, settle_ticks: int = 0) -> None:
        """A rolling update that progresses: ``unready`` pods are not ready yet, and one
        more replica is updated every tick; once all are updated, the last pods take
        ``settle_ticks`` more ticks to become ready (a slow pod, a DaemonSet's last node)."""
        d = self.deployments[deployment]
        d["generation"] += 1
        d.update(updated=0, unready=unready, rolling=True, settle=settle_ticks)

    def pause(self, deployment: str) -> None:
        """``kubectl rollout pause``: the rollout waits where it is, on purpose."""
        self.deployments[deployment]["paused"] = True

    def scale(self, deployment: str, replicas: int, unready: int = 0) -> None:
        d = self.deployments[deployment]
        d.update(replicas=replicas, unready=unready)

    def restart(self, deployment: str) -> None:
        """``kubectl rollout restart``: only the template's restartedAt annotation changes."""
        d = self.deployments[deployment]
        d["template"]["metadata"].setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = iso(self.now)
        d["generation"] += 1

    def replace_pod(self, deployment: str) -> None:
        """Delete pod 0; its replacement runs the same template (not a rollout)."""
        self.deployments[deployment]["replaced"] += 1

    def warning(self, kind: str, name: str, reason: str, message: str, count: int = 1) -> None:
        for ev in self.events:
            if (ev["involvedObject"]["kind"], ev["involvedObject"]["name"], ev["reason"]) == (kind, name, reason):
                ev["count"] = count
                ev["lastTimestamp"] = iso(self.now)
                return
        self.events.append({
            "kind": "Event", "type": "Warning", "reason": reason, "message": message, "count": count,
            "metadata": {"name": f"{name}.{len(self.events)}", "namespace": NS},
            "involvedObject": {"kind": kind, "name": name, "namespace": NS},
            "lastTimestamp": iso(self.now),
        })

    def tick(self, seconds: float = 10.0) -> None:
        self.now += seconds
        for d in self.deployments.values():
            if d["fault"]:
                d["restarts"] += 1
            if d["rolling"]:
                if d["updated"] is not None and d["updated"] < d["replicas"]:
                    d["updated"] += 1
                elif d.get("settle", 0) > 0:
                    d["settle"] -= 1  # all updated, the last pods still starting
                else:
                    d.update(updated=None, unready=0, rolling=False)

    # -- rendering -----------------------------------------------------------
    def revision(self, name: str) -> str:
        return _hash(self.deployments[name]["template"])

    def pod_name(self, name: str, i: int) -> str:
        d = self.deployments[name]
        suffix = f"{i:04d}{d['replaced'] if i == 0 else 0}"
        return f"{name}-{self.revision(name)}-p{suffix}"

    def _pods(self, name: str) -> list[dict[str, Any]]:
        d = self.deployments[name]
        rev = self.revision(name)
        fault = d["fault"]
        out = []
        for i in range(d["replicas"]):
            ready = fault is None and i >= d["unready"]
            status: dict[str, Any] = {"name": name, "ready": ready, "restartCount": d["restarts"],
                                      "state": {"running": {}}, "lastState": {}}
            if fault:
                status["state"] = {"waiting": {"reason": fault["waiting"], "message": "back-off restarting"}}
                status["lastState"] = {"terminated": {"reason": fault["reason"], "exitCode": fault["exit_code"]}}
            out.append({
                "kind": "Pod",
                "metadata": {"name": self.pod_name(name, i), "namespace": NS,
                             "creationTimestamp": iso(d["created"]),
                             "labels": {**d["labels"], "pod-template-hash": rev},
                             "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-{rev}"}]},
                "spec": copy.deepcopy(d["template"]["spec"]),
                "status": {"phase": "Running",
                           "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                           "containerStatuses": [status]},
            })
        return out

    def render(self, *, endpoint_slices: bool = True) -> dict[str, dict[str, list[dict[str, Any]]]]:
        pods, deployments, replicasets = [], [], []
        for name, d in self.deployments.items():
            own = self._pods(name)
            pods += own
            ready = sum(1 for p in own if p["status"]["conditions"][0]["status"] == "True")
            deployments.append({
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": NS, "generation": d["generation"],
                             "creationTimestamp": iso(d["created"])},
                "spec": {"replicas": d["replicas"], "selector": {"matchLabels": d["labels"]},
                         "template": copy.deepcopy(d["template"]), "strategy": copy.deepcopy(d["strategy"]),
                         **({"paused": True} if d["paused"] else {})},
                "status": {"readyReplicas": ready,
                           "updatedReplicas": d["replicas"] if d["updated"] is None else d["updated"],
                           "observedGeneration": d["generation"],
                           # Like a DaemonSet, report "converged" as soon as every pod is
                           # updated, even while the last ones are still starting.
                           "conditions": [{"type": "Progressing", "status": "True",
                                           "reason": "NewReplicaSetAvailable"
                                           if d["updated"] is None or d["updated"] >= d["replicas"]
                                           else "ReplicaSetUpdated"}]},
            })
            replicasets.append({"kind": "ReplicaSet", "metadata": {
                "name": f"{name}-{self.revision(name)}", "namespace": NS,
                "ownerReferences": [{"kind": "Deployment", "name": name}]}})
        services, slices, endpoints = [], [], []
        for name, s in self.services.items():
            services.append({"kind": "Service",
                             "metadata": {"name": name, "namespace": NS, "creationTimestamp": iso(T0 - 86_400)},
                             "spec": {"selector": s["selector"], "type": "ClusterIP",
                                      "ports": [{"port": s["port"], "targetPort": s["target"], "protocol": "TCP"}]}})
            matched = [p for p in pods if all(p["metadata"]["labels"].get(k) == v for k, v in s["selector"].items())]
            ready = [p for p in matched if p["status"]["conditions"][0]["status"] == "True"]
            slices.append({"kind": "EndpointSlice",
                           "metadata": {"name": f"{name}-x", "namespace": NS,
                                        "labels": {"kubernetes.io/service-name": name}},
                           "endpoints": [{"conditions": {"ready": p in ready},
                                          "targetRef": {"kind": "Pod", "name": p["metadata"]["name"], "namespace": NS}}
                                         for p in matched]})
            endpoints.append({"kind": "Endpoints", "metadata": {"name": name, "namespace": NS},
                              "subsets": [{"addresses": [
                                  {"targetRef": {"kind": "Pod", "name": p["metadata"]["name"], "namespace": NS}}
                                  for p in ready]}]})
        configmaps = [{"kind": "ConfigMap", "metadata": {"name": n, "namespace": NS,
                                                         "creationTimestamp": iso(T0 - 86_400)},
                       "data": c["data"]} for n, c in self.configmaps.items()]
        raw = {
            "pods": {"items": pods}, "deployments": {"items": deployments},
            "replicasets": {"items": replicasets}, "services": {"items": services},
            "events": {"items": copy.deepcopy(self.events)}, "configmaps": {"items": configmaps},
            "statefulsets": {"items": []}, "daemonsets": {"items": []},
        }
        for resource, items in self.objects.items():
            raw[resource] = {"items": copy.deepcopy(items)}
        raw["endpointslices" if endpoint_slices else "endpoints"] = {"items": slices if endpoint_slices else endpoints}
        return raw

    def kubectl_list(self) -> str:
        """The combined ``kubectl get <kinds> -A -o json`` output."""
        items = [i for value in self.render().values() for i in value["items"]]
        return json.dumps({"kind": "List", "items": items})

    def kubectl_get(self, command: str) -> str:
        """``kubectl get <kinds> ... -o json`` as kubectl answers it: only the kinds asked
        for, with v1 Endpoints (what the kubectl-agent's RBAC grants), as one List."""
        kinds = set(command.split()[1].split(","))
        items = [i for resource, value in self.render(endpoint_slices=False).items()
                 if resource in kinds for i in value["items"]]
        return json.dumps({"kind": "List", "items": items})


def shop() -> Cluster:
    """The Aurora Lab ``shop`` app, healthy: loadgen -> frontend -> api -> redis, plus a worker."""
    c = Cluster()
    c.configmap("api-code", {"api.py": "print('api')", "worker.py": "print('worker')"})
    c.configmap("frontend-nginx", {"default.conf": "upstream api { server api:8080; }"})
    c.deployment("redis")
    c.deployment("api", replicas=2, env={"REDIS_HOST": "redis"}, configmap="api-code")
    c.deployment("worker", env={"BATCH_SIZE": "500"}, configmap="api-code")
    c.deployment("frontend", replicas=2, configmap="frontend-nginx")
    c.deployment("loadgen")
    c.service("redis", {"app": "redis"}, 6379, 6379)
    c.service("api", {"app": "api"}, 8080, 8080)
    c.service("frontend", {"app": "frontend"}, 80, 80)
    return c


def healthy_logs() -> dict[str, list[str]]:
    return {
        "Deployment/shop/loadgen": ["2026-11-17T21:44:51Z GET /api/items -> 200"] * 3,
        "Deployment/shop/frontend": ['10.244.0.9 - - [17/Nov/2026:21:44:51 +0000] "GET /api/items HTTP/1.1" 200 38 "-" "Wget" "-"'] * 3,
        "Deployment/shop/api": ['10.244.0.7 "GET /items HTTP/1.0" 200 -'] * 3,
        "Deployment/shop/worker": ["processed batch of 500 items"] * 2,
        "Deployment/shop/redis": [],
    }

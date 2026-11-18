"""What changed in the cluster between two snapshots, in words an engineer or an RCA can use.

Each change records *when* the object was written (from managedFields, so a
change that happened between two polls still gets its real time) and a short
list of human-readable details, for example
``container worker: env BATCH_SIZE 500 -> 2500`` or
``selector {app: frontend} -> {app: frontend, tier: web}``.
"""

from __future__ import annotations

from typing import Any

MAX_DETAILS = 6
MAX_DETAIL_CHARS = 300
_MAX_ITEMS = 10


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _fmt(value: Any) -> str:
    """A value as a short line: lists and dicts show their first items, with a count."""
    if isinstance(value, (dict, list, tuple)):
        items = sorted(value.items()) if isinstance(value, dict) else list(value)
        parts = [f"{k}: {_fmt(v)}" for k, v in items[:_MAX_ITEMS]] if isinstance(value, dict) else [
            _fmt(v) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            parts.append(f"+{len(items) - _MAX_ITEMS} more")
        open_, close = ("{", "}") if isinstance(value, dict) else ("[", "]")
        return _clip(open_ + ", ".join(parts) + close, MAX_DETAIL_CHARS)
    return _clip(str(value), 80)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: value}


def _dict_diff(old: dict[str, Any] | None, new: dict[str, Any] | None, label: str) -> list[str]:
    a, b = _flatten(old or {}), _flatten(new or {})
    return [
        f"{label}.{key}: {_fmt(a.get(key, '<unset>'))} -> {_fmt(b.get(key, '<unset>'))}"
        for key in sorted(set(a) | set(b))
        if a.get(key) != b.get(key)
    ]


def _container_diff(kind: str, name: str, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    out = []
    if old.get("image") != new.get("image"):
        out.append(f"{kind} {name}: image {old.get('image')} -> {new.get('image')}")
    old_env, new_env = old.get("env") or {}, new.get("env") or {}
    for var in sorted(set(old_env) | set(new_env)):
        if var not in old_env:
            out.append(f"{kind} {name}: env {var} added = {new_env[var]}")
        elif var not in new_env:
            out.append(f"{kind} {name}: env {var} removed (was {old_env[var]})")
        elif old_env[var] != new_env[var]:
            out.append(f"{kind} {name}: env {var} {old_env[var]} -> {new_env[var]}")
    for field in ("envFrom", "command", "args", "ports"):
        if old.get(field) != new.get(field):
            out.append(f"{kind} {name}: {field} {_fmt(old.get(field))} -> {_fmt(new.get(field))}")
    for field in ("resources", "livenessProbe", "readinessProbe", "startupProbe"):
        out += [f"{kind} {name}: {d}" for d in _dict_diff(old.get(field), new.get(field), field)]
    return out


def diff_template(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Readable differences between two pod-template summaries."""
    out: list[str] = []
    for group, label in (("containers", "container"), ("initContainers", "init container")):
        a, b = old.get(group) or {}, new.get(group) or {}
        for name in sorted(set(a) | set(b)):
            if name not in a:
                limits = ((b[name].get("resources") or {}).get("limits") or {})
                extra = f", limits {_fmt(limits)}" if limits else ""
                out.append(f"{label} {name} added (image {b[name].get('image')}{extra})")
            elif name not in b:
                out.append(f"{label} {name} removed")
            else:
                out += _container_diff(label, name, a[name], b[name])
    a, b = old.get("volumes") or {}, new.get("volumes") or {}
    for name in sorted(set(a) | set(b)):
        if a.get(name) != b.get(name):
            out.append(f"volume {name}: {a.get(name, '<none>')} -> {b.get(name, '<none>')}")
    for field in ("dnsPolicy", "dnsConfig", "nodeSelector", "serviceAccountName"):
        if old.get(field) != new.get(field):
            out.append(f"{field}: {_fmt(old.get(field))} -> {_fmt(new.get(field))}")
    if new.get("restartedAt") and old.get("restartedAt") != new.get("restartedAt"):
        out.append(f"rollout restart requested at {new.get('restartedAt')}")
    return out


def _change(kind: str, namespace: str, name: str, action: str, details: list[str],
            at: float | None, window: tuple[float, float], **extra: Any) -> dict[str, Any]:
    """One change record. ``at`` is clamped into the poll window that saw it.

    A change found by diffing two snapshots happened between them, whatever the
    object's own timestamps say: ``kubectl get`` omits managedFields, so a
    modified object only offers its creationTimestamp, which can be days old.
    """
    start, end = window
    when = min(max(at if at is not None else start, start), end)
    return {
        "key": f"{kind}/{namespace}/{name}", "kind": kind, "namespace": namespace, "name": name,
        "action": action, "details": [_clip(d, MAX_DETAIL_CHARS) for d in details[:MAX_DETAILS]], "at": when,
        "detected_at": end, **extra,
    }


def _created_detail(obj: dict[str, Any]) -> str:
    """What a "created" change says about the new object: a ConfigMap's keys (the first
    few and how many), other kinds' spec summary."""
    detail = obj.get("detail")
    if obj.get("kind") == "ConfigMap" and isinstance(detail, dict):
        first = detail.get("first") or []
        more = (detail.get("keys") or 0) - len(first)
        return f" with keys {', '.join(first)}{f' and {more} more' if more > 0 else ''}" if first else ""
    return f" {_fmt(detail)}" if detail else ""


def _object_extra(obj: dict[str, Any]) -> dict[str, Any]:
    """What a change record keeps of the object for attribution: a NetworkPolicy's pod
    selector, a quota's limits. Not a ConfigMap's key list, which can be huge."""
    return {} if obj.get("kind") == "ConfigMap" else {"detail": obj.get("detail")}


def _fmt_ports(ports: list[list[str]]) -> str:
    return ", ".join(f"{p}->{t}/{proto}" for p, t, proto in ports)


def detect_changes(prev: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    """Changes between two snapshots, most recent write first."""
    now = cur["taken_at"]
    window = (prev.get("taken_at") or now, now)
    out: list[dict[str, Any]] = []

    pw, cw = prev.get("workloads") or {}, cur.get("workloads") or {}
    for key in sorted(set(pw) | set(cw)):
        old, new = pw.get(key), cw.get(key)
        if old is None:
            out.append(_change(
                new["kind"], new["namespace"], new["name"], "created",
                [f"{new['kind'].lower()} created with {new['desired']} replica(s), pod labels {_fmt(new['pod_labels'])}"],
                new.get("last_write"), window, pod_labels=new["pod_labels"]))
        elif new is None:
            out.append(_change(old["kind"], old["namespace"], old["name"], "deleted",
                               [f"{old['kind'].lower()} deleted"], None, window, pod_labels=old["pod_labels"]))
        else:
            details = []
            if old["template_hash"] != new["template_hash"]:
                details = diff_template(old["template"], new["template"]) or ["pod template changed"]
            if old["desired"] != new["desired"]:
                details.append(f"replicas {old['desired']} -> {new['desired']}")
            if details:
                out.append(_change(new["kind"], new["namespace"], new["name"], "changed", details,
                                   new.get("last_write"), window, pod_labels=new["pod_labels"]))

    ps, cs = prev.get("services") or {}, cur.get("services") or {}
    for key in sorted(set(ps) | set(cs)):
        old, new = ps.get(key), cs.get(key)
        if old is None:
            out.append(_change("Service", new["namespace"], new["name"], "created",
                               [f"service created, selector {_fmt(new['selector'])}"],
                               new.get("last_write"), window, selector=new["selector"]))
        elif new is None:
            out.append(_change("Service", old["namespace"], old["name"], "deleted",
                               ["service deleted"], None, window, selector=old["selector"]))
        elif old["spec_hash"] != new["spec_hash"]:
            details = []
            if old["selector"] != new["selector"]:
                details.append(f"selector {_fmt(old['selector'])} -> {_fmt(new['selector'])}")
            if old["ports"] != new["ports"]:
                details.append(f"ports {_fmt_ports(old['ports'])} -> {_fmt_ports(new['ports'])} (port->targetPort)")
            if old.get("type") != new.get("type"):
                details.append(f"type {old.get('type')} -> {new.get('type')}")
            out.append(_change("Service", new["namespace"], new["name"], "changed",
                               details or ["spec changed"], new.get("last_write"), window,
                               selector=new["selector"]))

    po, co = prev.get("objects") or {}, cur.get("objects") or {}
    for key in sorted(set(po) | set(co)):
        old, new = po.get(key), co.get(key)
        if old is None:
            detail = _created_detail(new)
            out.append(_change(new["kind"], new["namespace"], new["name"], "created",
                               [f"{new['kind']} created{detail}"], new.get("last_write"), window,
                               **_object_extra(new)))
        elif new is None:
            out.append(_change(old["kind"], old["namespace"], old["name"], "deleted",
                               [f"{old['kind']} deleted"], None, window, **_object_extra(old)))
        elif old["content"] != new["content"]:
            if isinstance(new["content"], dict):
                a, b = old["content"] or {}, new["content"]
                details = []
                for k in sorted(set(a) | set(b)):
                    if a.get(k) != b.get(k):
                        verb = "added" if k not in a else "removed" if k not in b else "changed"
                        details.append(f"{new['kind']} key {k} {verb}")
            else:
                details = [f"{new['kind']} spec changed {_fmt(new.get('detail'))}"]
            out.append(_change(new["kind"], new["namespace"], new["name"], "changed", details,
                               new.get("last_write"), window, **_object_extra(new)))

    # A pod replaced while its template stayed the same: a manual delete, an
    # eviction or a node problem. Config is read at start, so the new pod may run
    # different config than its siblings. The same pod-template-hash on both
    # sides is what separates this from a rolling update.
    prev_pods, cur_pods = prev.get("pods") or {}, cur.get("pods") or {}
    gone: dict[tuple[str, Any], list[str]] = {}
    for key, pod in prev_pods.items():
        if key not in cur_pods and pod.get("template_hash"):
            gone.setdefault((pod["workload"], pod["template_hash"]), []).append(pod["name"])
    for key, pod in sorted(cur_pods.items()):
        if key in prev_pods or not pod.get("template_hash"):
            continue
        slot = gone.get((pod["workload"], pod["template_hash"]))
        if not slot:
            continue
        siblings = sorted(p["name"] for k, p in cur_pods.items()
                          if p["workload"] == pod["workload"] and k != key and k in prev_pods)
        kind, ns, name = pod["workload"].split("/", 2)
        detail = f"pod {slot.pop(0)} replaced by {pod['name']} (pod template unchanged)"
        if siblings:
            detail += f"; {len(siblings)} other replica(s) kept running: {', '.join(siblings[:3])}"
        workload = (cur.get("workloads") or {}).get(pod["workload"]) or {}
        out.append(_change(kind, ns, name, "pod_replaced", [detail], pod.get("created"), window,
                           pod_labels=workload.get("pod_labels") or {}))

    out.sort(key=lambda c: c["at"], reverse=True)
    return out

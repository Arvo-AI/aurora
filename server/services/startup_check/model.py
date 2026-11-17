"""Data model for Startup Check: manifest index, findings, quantity parsing.

The scanner parses every YAML file in a repo into :class:`K8sObject`
records; rules query the resulting :class:`RepoIndex`. Namespaces are
approximated: two objects with explicit namespaces only match when equal;
objects without a namespace are treated as repo-scoped (matching any) —
the common case for repos of plain manifests. Findings note this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# Kubernetes workloads that carry a pod template.
_WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "Pod",
}

_MEM_SUFFIXES = [
    ("Ei", 2**60), ("Pi", 2**50), ("Ti", 2**40), ("Gi", 2**30),
    ("Mi", 2**20), ("Ki", 2**10),
    ("E", 10**18), ("P", 10**15), ("T", 10**12), ("G", 10**9),
    ("M", 10**6), ("k", 10**3),
]

def parse_memory(value: Any) -> Optional[int]:
    """Parse a Kubernetes memory quantity to bytes; None when unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    try:
        for suffix, mult in _MEM_SUFFIXES:
            if s.endswith(suffix):
                num = s[: -len(suffix)].strip()
                return int(float(num) * mult)
        return int(float(s))
    except (ValueError, TypeError):
        return None


def parse_cpu(value: Any) -> Optional[float]:
    """Parse a Kubernetes cpu quantity to cores; None when unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        if s.endswith("m"):
            return float(s[:-1].strip()) / 1000.0
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_mb(value: Any) -> Optional[float]:
    """Parse a quantity to megabytes (fractional); None when unparseable."""
    b = parse_memory(value)
    return None if b is None else b / (2**20)


def ns_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """Namespace approximation: explicit namespaces must agree."""
    if a and b:
        return a == b
    return True


@dataclass
class K8sObject:
    """One manifest document, flattened for rule access."""

    kind: str
    name: str
    namespace: Optional[str]
    manifest: Dict[str, Any]
    file_path: str
    line: Optional[int] = None

    @property
    def labels(self) -> Dict[str, str]:
        return (self.manifest.get("metadata") or {}).get("labels") or {}

    @property
    def pod_spec(self) -> Optional[Dict[str, Any]]:
        """The pod spec this object (workload or Pod) runs, if any."""
        if self.kind == "Pod":
            return self.manifest.get("spec")
        if self.kind == "CronJob":
            spec = (self.manifest.get("spec") or {}).get("jobTemplate", {})
            spec = (spec.get("spec") or {}).get("template", {})
            return spec.get("spec")
        template = (self.manifest.get("spec") or {}).get("template")
        if isinstance(template, dict):
            return template.get("spec")
        return None

    @property
    def pod_labels(self) -> Dict[str, str]:
        """Labels of the pod template (used for Service selector matching)."""
        if self.kind == "Pod":
            return self.labels
        if self.kind == "CronJob":
            spec = (self.manifest.get("spec") or {}).get("jobTemplate", {})
            spec = (spec.get("spec") or {}).get("template", {})
            meta = spec.get("metadata") or {}
            return meta.get("labels") or {}
        template = (self.manifest.get("spec") or {}).get("template")
        if isinstance(template, dict):
            meta = template.get("metadata") or {}
            return meta.get("labels") or {}
        return {}

    @property
    def pod_name(self) -> str:
        """Best-effort identity for matching user context to workloads."""
        return (
            self.pod_labels.get("app")
            or self.pod_labels.get("app.kubernetes.io/name")
            or self.name
        )

    def containers(self) -> List[Dict[str, Any]]:
        spec = self.pod_spec or {}
        out = list(spec.get("containers") or [])
        out.extend(spec.get("initContainers") or [])
        return [c for c in out if isinstance(c, dict)]


@dataclass
class RepoIndex:
    """All parsed manifests from one repo, queryable by rules."""

    objects: List[K8sObject] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)

    @property
    def services(self) -> List[K8sObject]:
        return [o for o in self.objects if o.kind == "Service"]

    @property
    def workloads(self) -> List[K8sObject]:
        return [o for o in self.objects if o.kind in _WORKLOAD_KINDS]

    @property
    def configmaps(self) -> List[K8sObject]:
        return [o for o in self.objects if o.kind == "ConfigMap"]

    @property
    def secrets(self) -> List[K8sObject]:
        return [o for o in self.objects if o.kind == "Secret"]

    @property
    def pvcs(self) -> List[K8sObject]:
        return [o for o in self.objects if o.kind == "PersistentVolumeClaim"]

    @property
    def quotas(self) -> List[K8sObject]:
        return [o for o in self.objects if o.kind == "ResourceQuota"]

    def names(self, objs: Iterable[K8sObject]) -> List[str]:
        return [o.name for o in objs]

    def service_selector_matches(
        self, service: K8sObject, workload: K8sObject
    ) -> bool:
        """True when the workload's pod template carries the Service's selector."""
        selector = (
            ((service.manifest.get("spec") or {}).get("selector")) or {}
        )
        if not selector:
            return False
        pod_labels = workload.pod_labels
        return all(pod_labels.get(k) == v for k, v in selector.items())

    def workloads_for_service(self, service: K8sObject) -> List[K8sObject]:
        return [
            w
            for w in self.workloads
            if ns_compatible(service.namespace, w.namespace)
            and self.service_selector_matches(service, w)
        ]

    def has_service_named(self, name: str) -> bool:
        return any(o.name == name for o in self.services)


def container_ports(container: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Declared containerPorts (numeric or named) for one container."""
    return [p for p in (container.get("ports") or []) if isinstance(p, dict)]


def workload_ports(obj: K8sObject) -> List[Dict[str, Any]]:
    """All container ports declared across a workload's pod template."""
    out: List[Dict[str, Any]] = []
    for container in obj.containers():
        out.extend(container_ports(container))
    return out


def best_effort_line(file_text: str, obj: K8sObject) -> Optional[int]:
    """Best-effort 1-based line of the object's `name:` in its file."""
    name = obj.name
    if not name:
        return obj.line
    pattern = re.compile(r"^\s*name:\s*['\"]?" + re.escape(name) + r"['\"]?\s*$")
    for i, text_line in enumerate(file_text.splitlines(), start=1):
        if pattern.match(text_line):
            return i
    return obj.line


def make_finding(
    *,
    rule: str,
    severity: str,
    file_path: str,
    title: str,
    explanation: str,
    line: Optional[int] = None,
    end_line: Optional[int] = None,
    source: str = "baseline",
) -> Dict[str, Any]:
    """Finding shape — same fields as a change-gating finding, plus ids."""
    finding = {
        "id": f"{rule}:{file_path}:{line or 0}",
        "rule": rule,
        "severity": severity,
        "source": source,
        "file_path": file_path,
        "line": line,
        "end_line": end_line or line,
        "title": title,
        "explanation": explanation,
    }
    return finding

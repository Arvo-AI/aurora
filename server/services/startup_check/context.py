"""User-provided environment context for Startup Check (Layer 2).

Everything here is OPTIONAL: the scan must produce value with an empty
context; user input only adds or sharpens checks. The GUI form emits the
same JSON the upload endpoint accepts — one schema, two doors.

Schema (all keys optional, unknown keys ignored for forward-compat)::

    {
      "nodes": [{"name": "pool-a", "cpu_cores": 16, "memory_mb": 65536}],
      "service_memory_mb": {"worker": 150},
      "storage_classes": ["standard"],
      "default_storage_class": false,
      "cluster_domain": "cluster.local",
      "known_services": ["redis"],
      "known_configmaps": ["nginx-conf"],
      "known_secrets": ["api-keys"],
      "known_workloads": ["api"]
    }

- ``nodes`` enables request-vs-node-capacity (lab scenario 08).
- ``service_memory_mb`` enables limit-vs-measured-usage (scenarios 01/02).
- ``storage_classes`` / ``default_storage_class`` enable PVC checks (09).
- ``cluster_domain`` enables DNS search-domain checks (16).
- ``known_*`` lists suppress Layer 1 findings for objects that legitimately
  live outside this repository (other repos, Helm charts, ops tooling) —
  the "clear the code" escape hatch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Keep a pasted/uploaded context from becoming a memory or DoS surface.
_MAX_CONTEXT_BYTES = 32 * 1024
_MAX_LIST_ITEMS = 200


class ContextValidationError(ValueError):
    """Raised when the user context JSON is structurally invalid."""


def _require_str_list(value: Any, key: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContextValidationError(f"'{key}' must be a list of strings")
    if len(value) > _MAX_LIST_ITEMS:
        raise ContextValidationError(f"'{key}' has too many entries (> {_MAX_LIST_ITEMS})")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ContextValidationError(f"'{key}' entries must be non-empty strings")
        out.append(item.strip())
    return out


def _validate_nodes(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContextValidationError("'nodes' must be a list")
    if len(value) > _MAX_LIST_ITEMS:
        raise ContextValidationError("'nodes' has too many entries")
    out: List[Dict[str, Any]] = []
    for node in value:
        if not isinstance(node, dict):
            raise ContextValidationError("each entry in 'nodes' must be an object")
        cpu = node.get("cpu_cores")
        mem = node.get("memory_mb")
        if not isinstance(cpu, (int, float)) or cpu <= 0:
            raise ContextValidationError("'nodes[].cpu_cores' must be a positive number")
        if not isinstance(mem, (int, float)) or mem <= 0:
            raise ContextValidationError("'nodes[].memory_mb' must be a positive number")
        name = node.get("name", "")
        if not isinstance(name, str):
            raise ContextValidationError("'nodes[].name' must be a string")
        out.append({
            "name": name.strip(),
            "cpu_cores": float(cpu),
            "memory_mb": float(mem),
        })
    return out


def _validate_service_memory(value: Any) -> Dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ContextValidationError("'service_memory_mb' must be an object of {service: MB}")
    if len(value) > _MAX_LIST_ITEMS:
        raise ContextValidationError("'service_memory_mb' has too many entries")
    out: Dict[str, float] = {}
    for service, mb in value.items():
        if not isinstance(service, str) or not service.strip():
            raise ContextValidationError("'service_memory_mb' keys must be non-empty strings")
        if not isinstance(mb, (int, float)) or mb <= 0:
            raise ContextValidationError(
                f"'service_memory_mb[\"{service}\"]' must be a positive number (MB)"
            )
        out[service.strip()] = float(mb)
    return out


@dataclass
class UserContext:
    """Validated Layer 2 context. Defaults = empty = Layer 1 only."""

    nodes: List[Dict[str, Any]] = field(default_factory=list)
    service_memory_mb: Dict[str, float] = field(default_factory=dict)
    storage_classes: List[str] = field(default_factory=list)
    default_storage_class: Optional[bool] = None
    cluster_domain: Optional[str] = None
    known_services: List[str] = field(default_factory=list)
    known_configmaps: List[str] = field(default_factory=list)
    known_secrets: List[str] = field(default_factory=list)
    known_workloads: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.nodes
            or self.service_memory_mb
            or self.storage_classes
            or self.default_storage_class is not None
            or self.cluster_domain
        )

    def max_node_cpu_cores(self) -> Optional[float]:
        return max((n["cpu_cores"] for n in self.nodes), default=None)

    def max_node_memory_mb(self) -> Optional[float]:
        return max((n["memory_mb"] for n in self.nodes), default=None)

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialization-safe shape for storing alongside a scan result."""
        return {
            "nodes": self.nodes,
            "service_memory_mb": self.service_memory_mb,
            "storage_classes": self.storage_classes,
            "default_storage_class": self.default_storage_class,
            "cluster_domain": self.cluster_domain,
        }


def parse_user_context(data: Any) -> UserContext:
    """Validate a raw (JSON-decoded) context payload.

    Raises ContextValidationError on structural problems; silently ignores
    unknown keys so older scans keep working as the schema grows.
    """
    if data is None:
        return UserContext()
    if isinstance(data, (str, bytes)):
        if len(data) > _MAX_CONTEXT_BYTES:
            raise ContextValidationError("context payload too large")
        return UserContext()  # an empty/blank string means "no context"
    if not isinstance(data, dict):
        raise ContextValidationError("context must be a JSON object")

    ctx = UserContext()
    ctx.nodes = _validate_nodes(data.get("nodes"))
    ctx.service_memory_mb = _validate_service_memory(data.get("service_memory_mb"))
    ctx.storage_classes = _require_str_list(data.get("storage_classes"), "storage_classes")

    default_sc = data.get("default_storage_class")
    if default_sc is not None and not isinstance(default_sc, bool):
        raise ContextValidationError("'default_storage_class' must be true or false")
    ctx.default_storage_class = default_sc

    domain = data.get("cluster_domain")
    if domain is not None:
        if not isinstance(domain, str) or not domain.strip() or len(domain) > 253:
            raise ContextValidationError("'cluster_domain' must be a non-empty string")
        ctx.cluster_domain = domain.strip().strip(".")

    ctx.known_services = _require_str_list(data.get("known_services"), "known_services")
    ctx.known_configmaps = _require_str_list(data.get("known_configmaps"), "known_configmaps")
    ctx.known_secrets = _require_str_list(data.get("known_secrets"), "known_secrets")
    ctx.known_workloads = _require_str_list(data.get("known_workloads"), "known_workloads")
    return ctx

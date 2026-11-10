"""Walk dbt lineage from a failing node using only ``manifest.json``.

Upstream: nearest-first list of ancestors, and the first one that looks like
the real origin (it failed in this run, it is the relation the error says is
missing, or its docs still list the column the error says is missing while
its SQL no longer mentions it).

Downstream: the blast radius. Models, snapshots and exposures that depend on
the failing node (for a failing test, on the model it tests, since
``dbt build`` skips everything below a failed test).
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Dict, List, Optional, Sequence

MAX_UPSTREAM = 60
MAX_DOWNSTREAM = 150
MAX_CODE_NODES = 8
MAX_CODE_CHARS = 5_000

_BUILT_TYPES = {"model", "snapshot", "seed"}
_FAILED = {"error", "fail", "runtime error"}


def _maps(manifest: Dict[str, Any]) -> tuple:
    parent_map = manifest.get("parent_map")
    child_map = manifest.get("child_map")
    if isinstance(parent_map, dict) and isinstance(child_map, dict):
        return parent_map, child_map
    # Older/partial manifests: rebuild from depends_on.
    parent_map, child_map = {}, {}
    for section in ("nodes", "exposures", "metrics", "semantic_models"):
        for uid, node in (manifest.get(section) or {}).items():
            deps = (node.get("depends_on") or {}).get("nodes") or []
            parent_map[uid] = list(deps)
            for d in deps:
                child_map.setdefault(d, []).append(uid)
    return parent_map, child_map


def _lookup(manifest: Dict[str, Any], uid: str) -> Dict[str, Any]:
    for section in ("nodes", "sources", "exposures", "metrics", "semantic_models", "unit_tests"):
        node = (manifest.get(section) or {}).get(uid)
        if node:
            return node
    return {}


def _rtype(manifest: Dict[str, Any], uid: str) -> str:
    return _lookup(manifest, uid).get("resource_type") or uid.split(".", 1)[0]


def _code(node: Dict[str, Any]) -> str:
    return (node.get("raw_code") or "") + "\n" + (node.get("compiled_code") or "")


def _mentions(code: str, ident: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(ident)}(?![\w])", code, re.IGNORECASE) is not None


def walk(
    manifest: Optional[Dict[str, Any]],
    start_ids: Sequence[str],
    run_statuses: Optional[Dict[str, str]] = None,
    missing_identifiers: Sequence[str] = (),
) -> Dict[str, Any]:
    """Return upstream/downstream lineage for the failing nodes.

    ``start_ids`` are failing unique_ids, most important first. Tests are
    walked from the model they test.
    """
    if not manifest or not start_ids:
        return {}
    run_statuses = run_statuses or {}
    parent_map, child_map = _maps(manifest)

    start = start_ids[0]
    anchor = start
    if _rtype(manifest, start) == "test":
        node = _lookup(manifest, start)
        anchor = node.get("attached_node") or next(
            (p for p in parent_map.get(start, []) if not p.startswith("test.")), start
        )

    upstream = _bfs(anchor, parent_map, MAX_UPSTREAM)
    downstream = _bfs(anchor, child_map, MAX_DOWNSTREAM)

    up_list = [
        {
            "unique_id": uid,
            "resource_type": _rtype(manifest, uid),
            "distance": dist,
            "run_status": run_statuses.get(uid, "not in this run"),
        }
        for uid, dist in upstream
    ]
    sources = [u["unique_id"] for u in up_list if u["resource_type"] == "source"]

    suspicious = _first_suspicious(
        manifest, anchor, upstream, run_statuses, missing_identifiers, parent_map,
        anchor_is_tested_model=anchor != start,
    )

    down_models = [uid for uid, _ in downstream if _rtype(manifest, uid) in _BUILT_TYPES]
    down_exposures = [uid for uid, _ in downstream if _rtype(manifest, uid) == "exposure"]
    down_other = [
        uid for uid, _ in downstream
        if _rtype(manifest, uid) in ("metric", "semantic_model", "saved_query")
    ]
    down_tests = sum(1 for uid, _ in downstream if _rtype(manifest, uid) == "test")
    direct_parents = [p for p in parent_map.get(anchor, []) if not p.startswith("test.")]

    # The cause often sits in a parent's SQL (e.g. a lazy view whose bad cast
    # only explodes when the child selects from it), so carry that code along.
    code_ids = [anchor] + direct_parents + ([suspicious["unique_id"]] if suspicious else [])
    upstream_code: Dict[str, str] = {}
    for uid in code_ids:
        if uid in upstream_code or len(upstream_code) >= MAX_CODE_NODES:
            continue
        node = _lookup(manifest, uid)
        raw = node.get("raw_code")
        if raw and node.get("resource_type") in ("model", "snapshot"):
            upstream_code[uid] = raw[:MAX_CODE_CHARS]

    return {
        "start": start,
        "anchor": anchor,
        "direct_parents": direct_parents,
        "upstream_code": upstream_code,
        "upstream": up_list,
        "upstream_sources": sources,
        "first_suspicious_ancestor": suspicious,
        "downstream_models": down_models,
        "downstream_exposures": down_exposures,
        "downstream_semantic": down_other,
        "downstream_tests": down_tests,
        "skipped_in_run": [uid for uid, st in run_statuses.items() if st == "skipped"][:100],
    }


def _bfs(start: str, edges: Dict[str, List[str]], limit: int) -> List[tuple]:
    seen = {start}
    out: List[tuple] = []
    queue = deque([(start, 0)])
    while queue and len(out) < limit:
        uid, dist = queue.popleft()
        for nxt in edges.get(uid, []) or []:
            if nxt in seen:
                continue
            seen.add(nxt)
            out.append((nxt, dist + 1))
            queue.append((nxt, dist + 1))
    return out


def _first_suspicious(
    manifest: Dict[str, Any],
    anchor: str,
    upstream: List[tuple],
    run_statuses: Dict[str, str],
    missing_identifiers: Sequence[str],
    parent_map: Dict[str, List[str]],
    anchor_is_tested_model: bool = False,
) -> Optional[Dict[str, Any]]:
    """Nearest ancestor that most likely holds the real cause, with the reason."""
    idents = [i.split(".")[-1] for i in missing_identifiers if i]
    candidates: List[tuple] = []  # (priority, distance, uid, reason)

    # When a test failed, the model it tests (the anchor, distance 0) is the
    # first suspect: a renamed column shows up as its own tests erroring.
    walk_order = list(upstream)
    if anchor_is_tested_model:
        walk_order = [(anchor, 0)] + walk_order

    for uid, dist in walk_order:
        if uid.startswith("test."):
            continue
        status = run_statuses.get(uid)
        if status in _FAILED:
            candidates.append((0, dist, uid, f"it also failed in this run ({status})"))
            continue
        node = _lookup(manifest, uid)
        names = {str(node.get(k) or "").lower() for k in ("name", "alias", "identifier")}
        for ident in idents:
            if ident.lower() in names:
                candidates.append((1, dist, uid, f"it is the relation '{ident}' the error says is missing"))
        code = _code(node)
        columns = {c.lower() for c in (node.get("columns") or {})}
        if code.strip():
            for ident in idents:
                if ident.lower() in columns and not _mentions(code, ident):
                    candidates.append((
                        2, dist, uid,
                        f"its docs still list column '{ident}' but its SQL no longer mentions it "
                        f"(renamed or dropped upstream?)",
                    ))
        if status == "skipped":
            candidates.append((4, dist, uid, "it was skipped in this run because something above it failed"))

    # Direct parents that do not produce the missing column at all, when no
    # stronger signal exists: the failing model expects it from one of them.
    if idents and not any(c[0] <= 2 for c in candidates):
        for parent in parent_map.get(anchor, []):
            node = _lookup(manifest, parent)
            if node.get("resource_type") not in _BUILT_TYPES:
                continue
            code = _code(node)
            if code.strip() and not any(_mentions(code, i) for i in idents):
                candidates.append((3, 1, parent, f"direct parent whose SQL never mentions {', '.join(idents)}"))

    if not candidates:
        return None
    pri, dist, uid, reason = sorted(candidates)[0]
    node = _lookup(manifest, uid)
    return {
        "unique_id": uid,
        "distance": dist,
        "reason": reason,
        "file_path": node.get("original_file_path"),
        "strength": {0: "strong", 1: "strong", 2: "medium", 3: "weak", 4: "weak"}[pri],
    }

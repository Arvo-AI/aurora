"""Glue: evidence -> classification + lineage, and the RCA hand-off text."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from services.pipeline_intake.classifier import classify
from services.pipeline_intake.evidence import PipelineFailureEvidence
from services.pipeline_intake.lineage import walk
from utils.security.output_redaction import redact

_PROMPT_SQL_CHARS = 6_000
_PROMPT_ERROR_CHARS = 3_000
_PROMPT_CHANGES_CHARS = 8_000
_PROMPT_LOG_LINES = 25
_PROMPT_UPSTREAM_CODE_CHARS = 12_000


def triage(
    evidence: PipelineFailureEvidence,
    manifest: Optional[Dict[str, Any]] = None,
    run_statuses: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Classify the failure and walk lineage. Deterministic and cheap (no I/O)."""
    started = time.perf_counter()
    classification = classify(evidence)

    if manifest:
        order = [classification.primary_node] if classification.primary_node else []
        order += [k for k in evidence.node_keys() if k not in order]
        dbt_lineage = walk(manifest, order, run_statuses, classification.identifiers)
        # Model SQL came from the caller's manifest: scrub it like every other field.
        code = dbt_lineage.get("upstream_code") or {}
        for uid, raw in list(code.items()):
            cleaned, findings = redact(raw)
            code[uid] = cleaned
            evidence.redactions += len(findings)
        evidence.lineage = {**evidence.lineage, **dbt_lineage}

    return {
        "category": classification.category,
        "confidence": classification.confidence,
        "rule_id": classification.rule_id,
        "primary_node": classification.primary_node,
        "evidence_lines": classification.evidence_lines,
        "note": classification.note,
        "identifiers": classification.identifiers,
        "nodes": [n.__dict__ for n in classification.nodes],
        "classifier_version": classification.classifier_version,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def blast_radius(evidence: PipelineFailureEvidence) -> Dict[str, Any]:
    lin = evidence.lineage or {}
    return {
        "models": lin.get("downstream_models", []),
        "exposures": lin.get("downstream_exposures", []),
        "semantic": lin.get("downstream_semantic", []),
        "tests": lin.get("downstream_tests", 0),
        "tasks": lin.get("downstream_tasks", []),
        "skipped_in_run": evidence.skipped_nodes[:50],
    }


def rca_payload(evidence: PipelineFailureEvidence, tri: Dict[str, Any], history: Dict[str, Any]) -> Dict[str, Any]:
    """The compact JSON the RCA prompt carries (full detail stays drillable via get_alert_field)."""
    primary = next((n for n in evidence.failing_nodes if n.unique_id == tri.get("primary_node")), None)
    primary = primary or evidence.primary_node()
    return {
        "title": evidence.title,
        "source_kind": evidence.source_kind,
        "pipeline": evidence.pipeline_name,
        "run_id": evidence.run_id,
        "pre_classification": {
            "category": tri["category"],
            "confidence": tri["confidence"],
            "rule_id": tri["rule_id"],
            "evidence_lines": tri["evidence_lines"],
            "note": tri.get("note"),
        },
        "primary_failing_node": primary.unique_id if primary else None,
        "failing_nodes": [
            {"unique_id": n.unique_id, "status": n.status, "file_path": n.file_path}
            for n in evidence.failing_nodes
        ],
        "first_suspicious_ancestor": (evidence.lineage or {}).get("first_suspicious_ancestor"),
        "blast_radius": blast_radius(evidence),
        "history": history,
        "context": {k: v for k, v in (evidence.context or {}).items() if k != "airflow_task_error"},
    }


def render_prompt_section(evidence: PipelineFailureEvidence, tri: Dict[str, Any], history: Dict[str, Any]) -> str:
    """Readable evidence block appended to the standard RCA prompt."""
    lines: List[str] = ["", "## PIPELINE FAILURE EVIDENCE (deterministic pre-triage, not yet verified)"]
    lines.append(
        f"Pre-classification: {tri['category']} (confidence {tri['confidence']}, rule {tri['rule_id']})"
    )
    for ev_line in tri.get("evidence_lines") or []:
        lines.append(f"  evidence: {ev_line}")
    if tri.get("note"):
        lines.append(f"  note: {tri['note']}")
    lines.append(f"History: {history.get('summary', 'no earlier submissions for these nodes')}")
    if evidence.previous_run:
        lines.append(f"Previous run: {evidence.previous_run}")

    for node in evidence.failing_nodes[:8]:
        lines.append("")
        lines.append(f"### Failing node: {node.unique_id} [{node.resource_type}, status={node.status}]")
        if node.file_path:
            lines.append(f"File: {node.file_path}")
        if node.test_name:
            lines.append(f"Test: {node.test_name} on column {node.test_column or '?'} of {node.attached_node or '?'}"
                         + (f"; failing rows: {node.failures}" if node.failures is not None else ""))
        if node.error_text:
            lines.append("Error:")
            lines.append("```")
            lines.append(node.error_text[:_PROMPT_ERROR_CHARS])
            lines.append("```")
        if node.compiled_sql:
            lines.append("Compiled SQL:")
            lines.append("```sql")
            lines.append(node.compiled_sql[:_PROMPT_SQL_CHARS])
            lines.append("```")

    lin = evidence.lineage or {}
    if lin.get("upstream") or lin.get("upstream_tasks") or lin.get("downstream_tasks"):
        lines.append("")
        lines.append("### Lineage")
        if lin.get("direct_parents"):
            lines.append(f"Direct parents: {', '.join(lin['direct_parents'])}")
        if lin.get("upstream"):
            nearest = [f"{u['unique_id']} (d={u['distance']}, {u['run_status']})" for u in lin["upstream"][:15]]
            lines.append(f"Upstream (nearest first): {'; '.join(nearest)}")
        if lin.get("upstream_sources"):
            lines.append(f"Upstream sources: {', '.join(lin['upstream_sources'][:15])}")
        sus = lin.get("first_suspicious_ancestor")
        lines.append(
            f"First suspicious ancestor: {sus['unique_id']} ({sus['strength']}): {sus['reason']}" if sus
            else "First suspicious ancestor: none found by heuristics"
        )
        if lin.get("upstream_tasks"):
            lines.append(f"Upstream Airflow tasks: {', '.join(lin['upstream_tasks'])}")
        br = blast_radius(evidence)
        lines.append(
            f"Blast radius: {len(br['models'])} downstream models, {len(br['exposures'])} exposures, "
            f"{br['tests']} tests, {len(br['tasks'])} downstream tasks"
        )
        if br["models"]:
            lines.append(f"  downstream models: {', '.join(br['models'][:20])}")
        if br["exposures"]:
            lines.append(f"  exposures: {', '.join(br['exposures'][:20])}")
        if br["tasks"]:
            lines.append(f"  downstream tasks: {', '.join(br['tasks'][:20])}")

    code = lin.get("upstream_code") or {}
    if code:
        lines.append("")
        lines.append("### Model source (raw SQL from manifest): failing node, direct parents, suspicious ancestor")
        budget = _PROMPT_UPSTREAM_CODE_CHARS
        for uid, raw in code.items():
            if budget <= 0:
                lines.append("(more parent SQL via get_alert_field('evidence.lineage.upstream_code'))")
                break
            snippet = raw[:budget]
            budget -= len(snippet)
            lines.append(f"-- {uid}")
            lines.append("```sql")
            lines.append(snippet)
            lines.append("```")

    if evidence.error_lines and evidence.source_kind != "dbt":
        lines.append("")
        lines.append("### Error lines from the log (newest last)")
        lines.append("```")
        lines.extend(evidence.error_lines[-_PROMPT_LOG_LINES:])
        lines.append("```")

    if evidence.recent_changes:
        lines.append("")
        lines.append("### Recent changes supplied by the user (e.g. git log -p for the failing model)")
        lines.append("```diff")
        lines.append(evidence.recent_changes[:_PROMPT_CHANGES_CHARS])
        lines.append("```")

    if evidence.truncated_fields:
        lines.append("")
        lines.append(f"(Truncated fields, full text via get_alert_field: {', '.join(evidence.truncated_fields[:10])})")
    return "\n".join(lines)


def rail_text(evidence: PipelineFailureEvidence) -> str:
    """Caller-authored text only, for the prompt-injection input rail."""
    parts = [evidence.title]
    for node in evidence.failing_nodes[:5]:
        if node.error_text:
            parts.append(node.error_text[:600])
    parts.extend(evidence.error_lines[-10:])
    if evidence.recent_changes:
        parts.append(evidence.recent_changes[:1000])
    return "\n\n".join(p for p in parts if p)[:3000]

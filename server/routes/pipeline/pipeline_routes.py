"""Pipeline door: investigate a failed dbt/Airflow/CI run with zero vendor credentials.

Two ways in, one evidence shape out:

* ``POST /pipeline/submit``        logged-in user uploads dbt artifacts, an
                                   Airflow payload, or pastes a log (RBAC).
* ``POST /pipeline/webhook/<uid>`` Airflow ``on_failure_callback`` pushes the
                                   failure, signed with an Aurora-issued
                                   HMAC secret (no Airflow credentials).

Both parse + redact + classify synchronously (no LLM), store one
``pipeline_failure_events`` row, answer with the triage, and hand the event to
``process_pipeline_failure`` which opens an incident and starts the normal
read-only background RCA with outbound notifications switched off.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from services.pipeline_intake.evidence import PipelineFailureEvidence
from services.pipeline_intake.parsers import (
    MAX_LOG_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RUN_RESULTS_BYTES,
    IntakeError,
    Scrubber,
    load_json_bytes,
    parse_airflow,
    parse_airflow_json_text,
    parse_dbt,
    parse_log,
    split_statuses,
)
from services.pipeline_intake.triage import blast_radius, triage
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize
from utils.web.webhook_signature import SIGNATURE_HEADER, verify_webhook_signature

logger = logging.getLogger(__name__)

pipeline_bp = Blueprint("pipeline", __name__)

PIPELINE_PROVIDER = "pipeline"
_HISTORY_DAYS = 30


# ---------------------------------------------------------------------------
# Intake helpers
# ---------------------------------------------------------------------------

def _too_large() -> Optional[Tuple[Any, int]]:
    length = request.content_length
    if length is not None and length > MAX_REQUEST_BYTES:
        return jsonify({"error": f"Request is larger than the {MAX_REQUEST_BYTES // (1024 * 1024)} MB limit"}), 413
    return None


def _file_bytes(name: str) -> Optional[bytes]:
    f = request.files.get(name)
    return f.read() if f else None


def _evidence_from_upload() -> Tuple[PipelineFailureEvidence, Optional[Dict[str, Any]], Dict[str, str]]:
    """Parse a UI/script submission (multipart form or JSON)."""
    scrub = Scrubber()
    if request.mimetype == "multipart/form-data":
        form = request.form
        kind = (form.get("kind") or "").strip().lower()
        run_results = load_json_bytes(_file_bytes("run_results"), "run_results.json", MAX_RUN_RESULTS_BYTES, False)
        manifest = load_json_bytes(_file_bytes("manifest"), "manifest.json", MAX_MANIFEST_BYTES, False)
        previous = load_json_bytes(_file_bytes("previous_run_results"), "previous run_results.json",
                                   MAX_RUN_RESULTS_BYTES, False)
        log_raw = _file_bytes("log_file")
        if log_raw is not None and len(log_raw) > MAX_LOG_BYTES:
            raise IntakeError(f"log file is larger than the {MAX_LOG_BYTES // (1024 * 1024)} MB limit", status=413)
        log_text = log_raw.decode("utf-8", "replace") if log_raw else form.get("log_text")
        airflow_text = form.get("airflow_payload")
        airflow = parse_airflow_json_text(airflow_text) if airflow_text else None
        recent_changes = form.get("recent_changes")
        pipeline_name = form.get("pipeline_name")
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise IntakeError("Send multipart/form-data or a JSON object")
        kind = str(data.get("kind") or "").strip().lower()
        run_results = data.get("run_results") if isinstance(data.get("run_results"), dict) else None
        manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else None
        previous = data.get("previous_run_results") if isinstance(data.get("previous_run_results"), dict) else None
        log_text = data.get("log_text") if isinstance(data.get("log_text"), str) else None
        airflow = data.get("airflow_payload") if isinstance(data.get("airflow_payload"), dict) else None
        recent_changes = data.get("recent_changes") if isinstance(data.get("recent_changes"), str) else None
        pipeline_name = data.get("pipeline_name") if isinstance(data.get("pipeline_name"), str) else None

    if not kind:
        kind = "dbt" if run_results else "airflow" if airflow else "log" if log_text else ""

    if kind == "dbt":
        if not run_results:
            raise IntakeError("run_results.json is required for a dbt submission")
        ev = parse_dbt(run_results, manifest, previous, recent_changes, scrub=scrub)
        if pipeline_name:
            ev.pipeline_name = pipeline_name.strip()[:200]
        ev, statuses = split_statuses(ev)
        return ev, manifest, statuses
    if kind == "airflow":
        if not airflow:
            raise IntakeError("airflow_payload is required for an Airflow submission")
        if recent_changes and not airflow.get("recent_changes"):
            airflow = {**airflow, "recent_changes": recent_changes}
        return _airflow_evidence(airflow, scrub)
    if kind == "log":
        if not log_text:
            raise IntakeError("log_text (or log_file) is required for a log submission")
        return parse_log(log_text, pipeline_name, recent_changes, scrub=scrub), None, {}
    raise IntakeError("Nothing to investigate: send dbt artifacts, an Airflow payload or log text")


def _airflow_evidence(payload: Dict[str, Any], scrub: Scrubber):
    ev = parse_airflow(payload, scrub=scrub)
    ev, statuses = split_statuses(ev)
    artifacts = payload.get("dbt_artifacts") if isinstance(payload.get("dbt_artifacts"), dict) else {}
    manifest = artifacts.get("manifest") if isinstance(artifacts.get("manifest"), dict) else None
    return ev, manifest, statuses


def _history(cursor, user_id: str, node_keys) -> Dict[str, Any]:
    """Has Aurora seen these nodes fail before? First failure vs repeat."""
    if not node_keys:
        return {"prior_failures": 0, "summary": "no node identity to compare"}
    cursor.execute(
        """SELECT COUNT(*), MAX(received_at) FROM pipeline_failure_events
           WHERE user_id = %s AND node_keys && %s::text[]
             AND received_at >= NOW() - (%s || ' days')::interval""",
        (user_id, list(node_keys), str(_HISTORY_DAYS)),
    )
    count, last = cursor.fetchone() or (0, None)
    if not count:
        return {"prior_failures": 0, "summary": f"first failure of these nodes seen by Aurora in {_HISTORY_DAYS} days"}
    return {
        "prior_failures": int(count),
        "last_seen": last.isoformat() if last else None,
        "summary": f"repeat: {count} earlier submission(s) for these nodes in {_HISTORY_DAYS} days, last at {last}",
    }


def _accept(
    user_id: str,
    evidence: PipelineFailureEvidence,
    manifest: Optional[Dict[str, Any]],
    statuses: Dict[str, str],
    intake: str,
    started: float,
):
    tri = triage(evidence, manifest, statuses)
    classification_ms = round((time.perf_counter() - started) * 1000, 1)

    from routes.pipeline.tasks import process_pipeline_failure

    received_at = datetime.now(timezone.utc)
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[PIPELINE]")
            if not org_id:
                return jsonify({"error": "No organization for this user"}), 403
            history = _history(cursor, user_id, evidence.node_keys())
            stored = {
                "evidence": evidence.to_dict(),
                "triage": tri,
                "history": history,
                "intake": intake,
                "classification_ms": classification_ms,
            }
            cursor.execute(
                """INSERT INTO pipeline_failure_events
                   (user_id, org_id, source_kind, intake, pipeline_name, run_id, title,
                    category, node_keys, payload, received_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    user_id, org_id, evidence.source_kind, intake,
                    evidence.pipeline_name[:255], (evidence.run_id or "")[:255] or None,
                    evidence.title, tri["category"], evidence.node_keys(),
                    json.dumps(stored, default=str), received_at,
                ),
            )
            event_id = cursor.fetchone()[0]
            conn.commit()

    process_pipeline_failure.delay(event_id, user_id)
    logger.info(
        "[PIPELINE] accepted event=%s kind=%s intake=%s category=%s nodes=%d redactions=%d classification_ms=%s",
        event_id, evidence.source_kind, intake, tri["category"], len(evidence.failing_nodes),
        evidence.redactions, classification_ms,
    )
    return jsonify({
        "event_id": event_id,
        "status": "accepted",
        "rca": "queued",
        "title": evidence.title,
        "source_kind": evidence.source_kind,
        "pipeline": evidence.pipeline_name,
        "category": tri["category"],
        "confidence": tri["confidence"],
        "rule_id": tri["rule_id"],
        "evidence_lines": tri["evidence_lines"],
        "note": tri.get("note"),
        "primary_node": tri["primary_node"],
        "failing_nodes": [
            {"unique_id": n.unique_id, "status": n.status, "file_path": n.file_path}
            for n in evidence.failing_nodes
        ],
        "first_suspicious_ancestor": (evidence.lineage or {}).get("first_suspicious_ancestor"),
        "blast_radius": blast_radius(evidence),
        "history": history,
        "redactions": evidence.redactions,
        "classification_ms": classification_ms,
    }), 202


# ---------------------------------------------------------------------------
# UI / script intake
# ---------------------------------------------------------------------------

@pipeline_bp.route("/submit", methods=["POST", "OPTIONS"])
@require_permission("incidents", "write")
def submit(user_id):
    """Upload dbt artifacts, an Airflow failure payload, or paste a log."""
    started = time.perf_counter()
    rejected = _too_large()
    if rejected:
        return rejected
    try:
        evidence, manifest, statuses = _evidence_from_upload()
    except IntakeError as e:
        return jsonify({"error": e.message}), e.status
    return _accept(user_id, evidence, manifest, statuses, "upload", started)


@pipeline_bp.route("/events/<int:event_id>", methods=["GET", "OPTIONS"])
@require_permission("incidents", "read")
def event_status(user_id, event_id: int):
    """Triage plus the RCA's progress and final answer for one submission."""
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            set_rls_context(cursor, conn, user_id, log_prefix="[PIPELINE:status]")
            cursor.execute(
                "SELECT payload, received_at FROM pipeline_failure_events WHERE id = %s",
                (event_id,),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404
            stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            cursor.execute(
                """SELECT id, status, aurora_status, aurora_summary, aurora_chat_session_id,
                          created_at, analyzed_at
                   FROM incidents WHERE source_type = %s AND source_alert_id = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (PIPELINE_PROVIDER, event_id),
            )
            inc = cursor.fetchone()
            final_message, session_status = None, None
            if inc and inc[4]:
                cursor.execute("SELECT messages, status FROM chat_sessions WHERE id = %s", (str(inc[4]),))
                srow = cursor.fetchone()
                if srow:
                    session_status = srow[1]
                    messages = srow[0] if isinstance(srow[0], list) else json.loads(srow[0] or "[]")
                    for msg in reversed(messages or []):
                        if isinstance(msg, dict) and msg.get("sender") == "bot" and msg.get("text"):
                            final_message = msg["text"]
                            break

    evidence = stored.get("evidence") or {}
    return jsonify({
        "event_id": event_id,
        "received_at": row[1].isoformat() if row[1] else None,
        "title": evidence.get("title"),
        "pipeline": evidence.get("pipeline_name"),
        "source_kind": evidence.get("source_kind"),
        "triage": stored.get("triage"),
        "history": stored.get("history"),
        "lineage": evidence.get("lineage"),
        "failing_nodes": [
            {k: n.get(k) for k in ("unique_id", "status", "file_path", "resource_type")}
            for n in evidence.get("failing_nodes") or []
        ],
        "classification_ms": stored.get("classification_ms"),
        "incident": None if not inc else {
            "id": str(inc[0]),
            "status": inc[1],
            "aurora_status": inc[2],
            "summary": inc[3],
            "chat_session_id": str(inc[4]) if inc[4] else None,
            "created_at": inc[5].isoformat() if inc[5] else None,
            "analyzed_at": inc[6].isoformat() if inc[6] else None,
        },
        "rca": {"session_status": session_status, "final_message": final_message},
    })


# ---------------------------------------------------------------------------
# Airflow webhook (HMAC with an Aurora-issued secret; no Airflow credentials)
# ---------------------------------------------------------------------------

def _get_webhook_secret(user_id: str) -> Optional[str]:
    try:
        data = get_token_data(user_id, PIPELINE_PROVIDER) or {}
    except Exception as e:
        logger.warning("[PIPELINE] webhook secret lookup failed: %s", type(e).__name__)
        return None
    return data.get("webhook_secret") or None


def _webhook_url(user_id: str) -> str:
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/") or request.host_url.rstrip("/")
    return f"{backend_url}/pipeline/webhook/{user_id}"


def _webhook_info(user_id: str, secret: Optional[str]) -> Dict[str, Any]:
    url = _webhook_url(user_id)
    return {
        "configured": bool(secret),
        "webhookUrl": url,
        "signatureHeader": SIGNATURE_HEADER,
        "secret": secret or "",
        "callbackSnippet": _callback_snippet(url, secret or "<generate a secret first>"),
        "instructions": [
            "1. Save the snippet as aurora_callback.py next to your DAGs.",
            "2. Set AURORA_PIPELINE_SECRET in the worker environment (or keep the inline default).",
            "3. Add on_failure_callback=aurora_on_failure to default_args (or to one task).",
            "4. For dbt tasks, pass params={'dbt_target_path': '/path/to/dbt/target'} so the callback "
            "attaches run_results.json and manifest.json.",
            "Aurora never calls Airflow: the callback pushes the failure, signed with this secret.",
        ],
    }


@pipeline_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    """Webhook URL, current secret (if generated) and the on_failure_callback snippet."""
    return jsonify(_webhook_info(user_id, _get_webhook_secret(user_id)))


@pipeline_bp.route("/webhook-secret", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def rotate_webhook_secret(user_id):
    """Create or rotate the Aurora-issued secret that signs Airflow callbacks."""
    secret = secrets.token_hex(32)
    store_tokens_in_db(user_id, {"webhook_secret": secret}, PIPELINE_PROVIDER, schedule_prediscovery=False)
    logger.info("[PIPELINE] webhook secret rotated for user %s", sanitize(user_id)[:50])
    return jsonify(_webhook_info(user_id, secret))


@pipeline_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive an Airflow on_failure_callback payload (HMAC-signed)."""
    started = time.perf_counter()
    if not user_id or len(user_id) > 255:
        return jsonify({"error": "Invalid webhook configuration"}), 403
    rejected = _too_large()
    if rejected:
        return rejected

    secret = _get_webhook_secret(user_id)
    if not secret:
        logger.warning("[PIPELINE] webhook rejected: no secret configured for %s", sanitize(user_id)[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not signature:
        return jsonify({"error": f"Missing {SIGNATURE_HEADER} header"}), 401
    if not verify_webhook_signature(request.get_data(), signature, secret):
        logger.warning("[PIPELINE] webhook rejected: bad signature for %s", sanitize(user_id)[:50])
        return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload format"}), 400
    try:
        evidence, manifest, statuses = _airflow_evidence(payload, Scrubber())
    except IntakeError as e:
        return jsonify({"error": e.message}), e.status
    return _accept(user_id, evidence, manifest, statuses, "webhook", started)


def _callback_snippet(url: str, secret: str) -> str:
    return f'''# aurora_callback.py  (no Airflow or warehouse credentials leave your worker)
import glob, hashlib, hmac, json, os, traceback, urllib.request

AURORA_WEBHOOK_URL = "{url}"
AURORA_PIPELINE_SECRET = os.environ.get("AURORA_PIPELINE_SECRET", "{secret}")


def _tail_log(ti, max_lines=200):
    try:
        from airflow.configuration import conf
        base = conf.get("logging", "base_log_folder")
        folder = os.path.join(base, f"dag_id={{ti.dag_id}}", f"run_id={{ti.run_id}}", f"task_id={{ti.task_id}}")
        files = sorted(glob.glob(os.path.join(folder, "**", "*.log"), recursive=True))
        if files:
            with open(files[-1], errors="replace") as fh:
                return "".join(fh.readlines()[-max_lines:])
    except Exception:
        pass
    return None


def _dbt_artifacts(context):
    target = (context.get("params") or {{}}).get("dbt_target_path")
    if not target:
        return None
    out = {{}}
    for name in ("run_results", "manifest"):
        path = os.path.join(target, f"{{name}}.json")
        if os.path.exists(path) and os.path.getsize(path) < 40 * 1024 * 1024:
            with open(path) as fh:
                out[name] = json.load(fh)
    return out or None


def aurora_on_failure(context):
    ti = context.get("ti") or context.get("task_instance")
    task = context.get("task")
    exc = context.get("exception")
    exc_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if isinstance(exc, BaseException) else str(exc or "")
    payload = {{
        "dag_id": ti.dag_id, "task_id": ti.task_id, "run_id": ti.run_id,
        "try_number": getattr(ti, "try_number", None), "max_tries": getattr(ti, "max_tries", None),
        "operator": getattr(task, "task_type", None) or type(task).__name__,
        "exception_type": type(exc).__name__ if exc else None, "exception": exc_text[-8000:],
        "log_excerpt": _tail_log(ti),
        "logical_date": str(context.get("logical_date") or context.get("ds") or ""),
        "start_date": str(getattr(ti, "start_date", "") or ""), "end_date": str(getattr(ti, "end_date", "") or ""),
        "duration": getattr(ti, "duration", None), "hostname": getattr(ti, "hostname", None),
        "upstream_task_ids": sorted(getattr(task, "upstream_task_ids", []) or []),
        "downstream_task_ids": sorted(getattr(task, "downstream_task_ids", []) or []),
        "dbt_artifacts": _dbt_artifacts(context),
    }}
    body = json.dumps(payload, default=str).encode()
    sig = hmac.new(AURORA_PIPELINE_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(AURORA_WEBHOOK_URL, data=body, method="POST",
                                 headers={{"Content-Type": "application/json", "{SIGNATURE_HEADER}": sig}})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # never fail the task because Aurora is unreachable
        print(f"[aurora] could not send failure payload: {{e}}")
'''

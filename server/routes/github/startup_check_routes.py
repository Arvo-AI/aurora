"""Startup Check — the repo check-engine light (routes + Celery task).

Read-only whole-repo scan that surfaces incident preconditions sourced
from aurora-lab's fault catalog. Reuses the GitHub auth router (App or
OAuth — no new credentials) and stores results in Redis with a TTL: the
check-engine light is a verdict, not a records system; suppression lists
("clearing the codes") live alongside the result for its lifetime.

API (all behind ``connectors:read`` RBAC — triggering a scan is a read):

    POST /github/startup-check/scans        {repo_full_name, context?}
                                          -> {scan_id, status: "queued"}
    GET  /github/startup-check/scans/<id>  -> full result while running/done
    PATCH /github/startup-check/scans/<id> {dismissed: [finding_id,...]}
                                          -> result with dismissals applied

The POST validates input and enqueues; the worker fetches the repo's YAML
manifests (bounded: <=60 files, <=200KB each, <=2MB total) and runs the
deterministic rule pipeline in ``services/startup_check``.
"""
import json
import logging
import re
import uuid

from celery_config import celery_app
from flask import Blueprint, jsonify, request

from services.startup_check.context import ContextValidationError, parse_user_context
from services.startup_check.context_llm import MAX_FACTS, MAX_FACT_CHARS
from services.startup_check.scanner import StartupCheckError
from utils.auth.rbac_decorators import require_permission
from utils.cache.redis_client import get_redis_client
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

startup_check_bp = Blueprint("startup_check", __name__)

_RESULT_TTL_SECONDS = 24 * 60 * 60
_REDIS_PREFIX = "startup_check:scan:"


def _result_key(scan_id: str) -> str:
    return f"{_REDIS_PREFIX}{scan_id}"


def _slug_is_valid(repo_full_name: str) -> bool:
    """``owner/repo`` shape only — keeps junk out of the GitHub API URL."""
    return bool(
        re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repo_full_name or "")
    )


def _load_result(scan_id: str, user_id: str):
    """Fetch a scan result owned by ``user_id``; None when missing/foreign."""
    if not re.fullmatch(r"[a-f0-9]{12}", scan_id or ""):
        return None
    client = get_redis_client()
    if client is None:
        return None
    raw = client.get(_result_key(scan_id))
    if not raw:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if result.get("user_id") != user_id:
        return None
    return result


def _store_result(scan_id: str, payload: dict) -> bool:
    client = get_redis_client()
    if client is None:
        logger.error("[STARTUP-CHECK] Redis unavailable; cannot persist scan %s", scan_id)
        return False
    client.set(_result_key(scan_id), json.dumps(payload), ex=_RESULT_TTL_SECONDS)
    return True


# ---------------------------------------------------------------------------
# Celery worker
# ---------------------------------------------------------------------------


@celery_app.task(name="routes.github.startup_check_routes.run_startup_check_task")
def run_startup_check_task(scan_id: str, user_id: str, repo_full_name: str, context: dict, freeform_facts: list = None):
    """Fetch the repo's manifests and run the check. Fails soft: every
    terminal state (including errors) is stored so the poll endpoint can
    tell the user exactly what happened instead of timing out."""
    payload = {
        "scan_id": scan_id,
        "user_id": user_id,
        "repo_full_name": repo_full_name,
        "status": "running",
    }
    _store_result(scan_id, payload)
    try:
        # Free-form facts go through the agent first; refusals are stored so
        # the UI can show why a fact was not used. Form fields always win
        # over interpreted ones — explicit input beats inference — but at the
        # SUBKEY level: form "worker: 700" must not drop the agent's
        # "api: 900" from the same dict.
        merged_context = dict(context or {})
        if freeform_facts:
            from services.startup_check.context_llm import parse_freeform_facts

            llm_ctx, refusals = parse_freeform_facts(user_id, freeform_facts)
            payload["context_refused"] = refusals
            for key, value in (llm_ctx or {}).items():
                existing = merged_context.get(key)
                if isinstance(existing, dict) and isinstance(value, dict):
                    merged = {**value, **existing}  # form subkeys win
                    merged_context[key] = merged
                elif isinstance(existing, list) and isinstance(value, list):
                    merged_context[key] = existing + [v for v in value if v not in existing]
                else:
                    merged_context.setdefault(key, value)
        ctx = parse_user_context(merged_context)
        from services.startup_check.scanner import scan_github_repo

        result = scan_github_repo(user_id, repo_full_name, ctx)
        payload.update(result)
        payload["status"] = "complete"
        payload["dismissed"] = []
    except StartupCheckError as exc:
        logger.warning(
            "[STARTUP-CHECK] scan %s failed for repo=%s: %s",
            scan_id, sanitize(repo_full_name), str(exc)[:300],
        )
        payload.update({"status": "error", "error": str(exc)})
    except ContextValidationError as exc:
        payload.update({"status": "error", "error": f"Invalid context: {exc}"})
    except Exception:
        logger.exception("[STARTUP-CHECK] scan %s crashed", scan_id)
        payload.update({
            "status": "error",
            "error": "Unexpected error while scanning — see server logs.",
        })
    _store_result(scan_id, payload)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@startup_check_bp.route("/scans", methods=["POST"])
@require_permission("connectors", "read")
def create_scan(user_id):
    """Enqueue a startup check for one ``owner/repo``."""
    data = request.get_json(silent=True) or {}
    repo_full_name = (data.get("repo_full_name") or "").strip()
    if not _slug_is_valid(repo_full_name):
        return jsonify({"error": "repo_full_name must be 'owner/repo'"}), 400

    context = data.get("context") or {}
    if isinstance(context, dict) and len(json.dumps(context)) > 32 * 1024:
        return jsonify({"error": "context payload too large"}), 400
    try:
        parse_user_context(context)
    except ContextValidationError as exc:
        return jsonify({"error": f"Invalid context: {exc}"}), 400

    # Free-form facts the agent interprets (and may refuse) in the task.
    freeform_facts = data.get("freeform_facts") or []
    if not isinstance(freeform_facts, list):
        return jsonify({"error": "freeform_facts must be a list"}), 400
    if len(freeform_facts) > MAX_FACTS:
        return jsonify({"error": f"freeform_facts has too many entries (> {MAX_FACTS})"}), 400
    clean_facts = []
    for fact in freeform_facts:
        if not isinstance(fact, dict):
            return jsonify({"error": "each freeform fact must be an object"}), 400
        clean_facts.append({
            "name": str(fact.get("name", ""))[:MAX_FACT_CHARS],
            "value": str(fact.get("value", ""))[:MAX_FACT_CHARS],
        })

    scan_id = uuid.uuid4().hex[:12]
    if not _store_result(scan_id, {
        "scan_id": scan_id,
        "user_id": user_id,
        "repo_full_name": repo_full_name,
        "status": "queued",
    }):
        return jsonify({"error": "Result store unavailable — is Redis up?"}), 503

    run_startup_check_task.delay(scan_id, user_id, repo_full_name, context, clean_facts)
    logger.info(
        "[STARTUP-CHECK] enqueued scan %s (user=%s repo=%s)",
        scan_id, sanitize(user_id), sanitize(repo_full_name),
    )
    return jsonify({"scan_id": scan_id, "status": "queued"}), 202


@startup_check_bp.route("/scans/<scan_id>", methods=["GET"])
@require_permission("connectors", "read")
def get_scan(user_id, scan_id):
    """Poll a scan: queued -> running -> complete | error."""
    result = _load_result(scan_id, user_id)
    if result is None:
        return jsonify({"error": "Scan not found"}), 404
    return jsonify(result)


@startup_check_bp.route("/scans/<scan_id>", methods=["PATCH"])
@require_permission("connectors", "read")
def patch_scan(user_id, scan_id):
    """Dismiss findings ("clear the codes"). Dismissal recomputes the
    verdict so a fully-cleared repo reads SAFE again."""
    result = _load_result(scan_id, user_id)
    if result is None:
        return jsonify({"error": "Scan not found"}), 404
    if result.get("status") != "complete":
        return jsonify({"error": "Only completed scans can be updated"}), 409

    data = request.get_json(silent=True) or {}
    dismissed = data.get("dismissed")
    if not isinstance(dismissed, list) or len(dismissed) > 500:
        return jsonify({"error": "dismissed must be a list of finding ids"}), 400

    valid_ids = {f.get("id") for f in result.get("findings", []) if isinstance(f, dict)}
    result["dismissed"] = [i for i in dismissed if isinstance(i, str) and i in valid_ids]

    active = [
        f for f in result.get("findings", [])
        if f.get("id") not in set(result["dismissed"])
    ]
    # NO_MANIFESTS is sticky: with no findings there is nothing to dismiss,
    # so a PATCH can only arrive here from a RISKY/SAFE scan — but guard
    # anyway so dismissal never rewrites a "nothing to check" verdict.
    if result.get("verdict") != "NO_MANIFESTS":
        result["verdict"] = "RISKY" if active else "SAFE"
    if not _store_result(scan_id, result):
        return jsonify({"error": "Result store unavailable"}), 503
    return jsonify(result)

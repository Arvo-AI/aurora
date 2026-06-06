import hashlib
import logging

import yaml
from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import resolve_org_id, set_rls_context
from utils.auth.token_management import store_tokens_in_db
from utils.db.connection_pool import db_pool
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.web.limiter_ext import limiter

kubeconfig_bp = Blueprint("kubeconfig_bp", __name__)
logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 500 * 1024  # 500KB per file
_ERR_NO_ORG = "Could not resolve organization"


def _make_cluster_id(org_id: str, context_name: str) -> str:
    digest = hashlib.sha256(f"{org_id}:{context_name}".encode()).hexdigest()[:16]
    return f"kubeconfig-{digest}"


def _make_vault_key(org_id: str, filename: str) -> str:
    digest = hashlib.sha256(f"{org_id}:{filename}".encode()).hexdigest()[:12]
    return f"kubeconfig_{digest}"


def _validate_kubeconfig(content: str) -> tuple:
    """Parse and validate a kubeconfig YAML. Returns (parsed_dict, error_string)."""
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return None, "Invalid YAML format"

    if not isinstance(parsed, dict):
        return None, "Kubeconfig must be a YAML mapping"

    for key in ("clusters", "contexts", "users"):
        if key not in parsed or not isinstance(parsed[key], list):
            return None, f"Missing or invalid '{key}' field"

    for user_entry in parsed.get("users", []):
        user_info = user_entry.get("user", {})
        if user_info.get("auth-provider"):
            return None, "auth-provider based authentication is not supported"

    return parsed, None


def _extract_contexts(parsed: dict) -> list:
    """Extract context metadata from a parsed kubeconfig."""
    cluster_map = {}
    for cluster_entry in parsed.get("clusters", []):
        name = cluster_entry.get("name")
        server = cluster_entry.get("cluster", {}).get("server")
        if name:
            cluster_map[name] = server

    contexts = []
    for ctx in parsed.get("contexts", []):
        ctx_name = ctx.get("name")
        ctx_info = ctx.get("context", {})
        if not ctx_name:
            continue
        contexts.append({
            "context_name": ctx_name,
            "cluster_name": ctx_info.get("cluster", ctx_name),
            "namespace": ctx_info.get("namespace"),
            "server_url": cluster_map.get(ctx_info.get("cluster")),
        })
    return contexts


def _process_kubeconfig_entry(entry, user_id, org_id, registered, errors):
    """Process a single kubeconfig entry. Appends to registered/errors in place."""
    if not isinstance(entry, dict):
        errors.append({"filename": "unknown", "error": "Invalid entry format"})
        return
    filename = entry.get("filename", "unknown.yaml")
    content = entry.get("content", "")
    if not isinstance(content, str):
        errors.append({"filename": filename, "error": "Content must be a string"})
        return

    if len(content.encode()) > MAX_FILE_SIZE:
        errors.append({"filename": filename, "error": f"File exceeds {MAX_FILE_SIZE // 1024}KB limit"})
        return

    parsed, err = _validate_kubeconfig(content)
    if err:
        errors.append({"filename": filename, "error": err})
        return

    contexts = _extract_contexts(parsed)
    if not contexts:
        errors.append({"filename": filename, "error": "No valid contexts found"})
        return

    vault_provider = _make_vault_key(org_id, filename)
    try:
        store_tokens_in_db(user_id, {"kubeconfig_yaml": content}, vault_provider, org_id=org_id)
    except Exception:
        logger.exception("[Kubeconfig] Vault store failed for %s", filename)
        errors.append({"filename": filename, "error": "Failed to store credentials"})
        return

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[Kubeconfig:upload]")
            for ctx in contexts:
                cluster_id = _make_cluster_id(org_id, ctx["context_name"])
                cur.execute("""
                    INSERT INTO kubeconfig_clusters
                        (user_id, org_id, cluster_id, context_name, cluster_name,
                         server_url, namespace, vault_provider, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (cluster_id) DO UPDATE SET
                        vault_provider = EXCLUDED.vault_provider,
                        server_url = EXCLUDED.server_url,
                        namespace = EXCLUDED.namespace,
                        is_active = TRUE,
                        updated_at = NOW()
                """, (user_id, org_id, cluster_id, ctx["context_name"],
                      ctx["cluster_name"], ctx["server_url"], ctx["namespace"],
                      vault_provider))
                registered.append({
                    "cluster_id": cluster_id,
                    "context_name": ctx["context_name"],
                    "cluster_name": ctx["cluster_name"],
                    "server_url": ctx["server_url"],
                })
            conn.commit()


@kubeconfig_bp.route("/api/kubeconfig/upload", methods=["POST"])
@limiter.limit("10 per minute;30 per hour")
@require_permission("connectors", "write")
def upload_kubeconfig(user_id):
    """Upload one or more kubeconfig files, store in Vault, register clusters."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    kubeconfigs = data.get("kubeconfigs", [])
    if not kubeconfigs or not isinstance(kubeconfigs, list):
        return jsonify({"error": "kubeconfigs array required"}), 400

    org_id = resolve_org_id(user_id)
    if not org_id:
        return jsonify({"error": _ERR_NO_ORG}), 400

    registered = []
    errors = []

    for entry in kubeconfigs:
        _process_kubeconfig_entry(entry, user_id, org_id, registered, errors)

    return jsonify({"registered": registered, "errors": errors}), 200 if registered else 400


@kubeconfig_bp.route("/api/kubeconfig/clusters", methods=["GET"])
@require_permission("connectors", "read")
def list_kubeconfig_clusters(user_id):
    """List all kubeconfig-sourced clusters for the user's org."""
    org_id = resolve_org_id(user_id)
    if not org_id:
        return jsonify({"error": _ERR_NO_ORG}), 400

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[Kubeconfig:list]")
            cur.execute("""
                SELECT cluster_id, context_name, cluster_name, server_url,
                       namespace, is_active, created_at, updated_at
                FROM kubeconfig_clusters
                WHERE org_id = %s AND is_active = TRUE
                ORDER BY cluster_name
            """, (org_id,))
            rows = cur.fetchall()

    clusters = [{
        "cluster_id": r[0], "context_name": r[1], "cluster_name": r[2],
        "server_url": r[3], "namespace": r[4], "is_active": r[5],
        "created_at": r[6].isoformat() if r[6] else None,
        "updated_at": r[7].isoformat() if r[7] else None,
    } for r in rows]

    return jsonify({"clusters": clusters})


@kubeconfig_bp.route("/api/kubeconfig/<cluster_id>", methods=["DELETE"])
@require_permission("connectors", "write")
def delete_kubeconfig_cluster(user_id, cluster_id):
    """Deactivate a kubeconfig cluster. Cleans up Vault if no other contexts share it."""
    org_id = resolve_org_id(user_id)
    if not org_id:
        return jsonify({"error": _ERR_NO_ORG}), 400

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[Kubeconfig:delete]")

            cur.execute("""
                SELECT vault_provider FROM kubeconfig_clusters
                WHERE cluster_id = %s AND org_id = %s
            """, (cluster_id, org_id))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Cluster not found"}), 404

            vault_provider = row[0]

            cur.execute("""
                UPDATE kubeconfig_clusters SET is_active = FALSE, updated_at = NOW()
                WHERE cluster_id = %s AND org_id = %s
            """, (cluster_id, org_id))

            # Check if any other active clusters share this vault secret
            cur.execute("""
                SELECT COUNT(*) FROM kubeconfig_clusters
                WHERE vault_provider = %s AND org_id = %s AND is_active = TRUE
            """, (vault_provider, org_id))
            remaining = cur.fetchone()[0]

            conn.commit()

    if remaining == 0:
        try:
            delete_user_secret(user_id, vault_provider)
        except Exception as e:
            logger.warning("[Kubeconfig] Vault cleanup failed for %s: %s", vault_provider, e)

    return jsonify({"deleted": cluster_id})

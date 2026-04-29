"""API routes for per-org multi-agent runtime tunables."""

import logging

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    set_rls_context,
)

logger = logging.getLogger(__name__)

multi_agent_config_bp = Blueprint("multi_agent_config", __name__, url_prefix="/api/settings")


_VALID_SEVERITIES = ("low", "medium", "high", "critical")

_COLUMNS = (
    "max_parallel_subagents",
    "max_total_subagents",
    "max_delegate_depth",
    "max_concurrent_rcas",
    "multi_agent_min_severity",
    "per_rca_token_budget",
    "per_subagent_token_budget",
    "per_rca_wallclock_seconds",
    "per_subagent_wallclock_seconds",
    "monthly_token_cap",
    "fallback_provider_chain",
)


def _defaults() -> dict:
    return {
        "max_parallel_subagents": 3,
        "max_total_subagents": 5,
        "max_delegate_depth": 1,
        "max_concurrent_rcas": 10,
        "multi_agent_min_severity": "medium",
        "per_rca_token_budget": 1500000,
        "per_subagent_token_budget": 300000,
        "per_rca_wallclock_seconds": 900,
        "per_subagent_wallclock_seconds": 240,
        "monthly_token_cap": None,
        "fallback_provider_chain": [],
    }


def _row_to_dict(row) -> dict:
    return {
        "max_parallel_subagents": row[0],
        "max_total_subagents": row[1],
        "max_delegate_depth": row[2],
        "max_concurrent_rcas": row[3],
        "multi_agent_min_severity": row[4],
        "per_rca_token_budget": row[5],
        "per_subagent_token_budget": row[6],
        "per_rca_wallclock_seconds": row[7],
        "per_subagent_wallclock_seconds": row[8],
        "monthly_token_cap": row[9],
        "fallback_provider_chain": list(row[10]) if row[10] is not None else [],
        "updated_at": row[11].isoformat() if row[11] else None,
    }


def _validate_int(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _validate_payload(data: dict, current: dict) -> tuple[dict | None, str | None]:
    cleaned: dict = {}

    for key in _COLUMNS:
        if key not in data:
            continue
        cleaned[key] = data[key]

    merged = {**current, **cleaned}

    if "max_parallel_subagents" in cleaned:
        if not _validate_int(cleaned["max_parallel_subagents"], 1, 10):
            return None, "max_parallel_subagents must be an int 1..10"

    if "max_total_subagents" in cleaned:
        if not _validate_int(cleaned["max_total_subagents"], 1, 20):
            return None, "max_total_subagents must be an int 1..20"

    if merged["max_total_subagents"] < merged["max_parallel_subagents"]:
        return None, "max_total_subagents must be >= max_parallel_subagents"

    if "max_delegate_depth" in cleaned:
        if not _validate_int(cleaned["max_delegate_depth"], 1, 3):
            return None, "max_delegate_depth must be an int 1..3"

    if "max_concurrent_rcas" in cleaned:
        if not _validate_int(cleaned["max_concurrent_rcas"], 1, 100):
            return None, "max_concurrent_rcas must be an int 1..100"

    if "multi_agent_min_severity" in cleaned:
        if cleaned["multi_agent_min_severity"] not in _VALID_SEVERITIES:
            return None, f"multi_agent_min_severity must be one of {list(_VALID_SEVERITIES)}"

    if "per_rca_token_budget" in cleaned:
        if not _validate_int(cleaned["per_rca_token_budget"], 100_000, 50_000_000):
            return None, "per_rca_token_budget must be an int 100000..50000000"

    if "per_subagent_token_budget" in cleaned:
        if not _validate_int(cleaned["per_subagent_token_budget"], 50_000, 10_000_000):
            return None, "per_subagent_token_budget must be an int 50000..10000000"

    if merged["per_subagent_token_budget"] > merged["per_rca_token_budget"]:
        return None, "per_subagent_token_budget must be <= per_rca_token_budget"

    if "per_rca_wallclock_seconds" in cleaned:
        if not _validate_int(cleaned["per_rca_wallclock_seconds"], 60, 7200):
            return None, "per_rca_wallclock_seconds must be an int 60..7200"

    if "per_subagent_wallclock_seconds" in cleaned:
        if not _validate_int(cleaned["per_subagent_wallclock_seconds"], 30, 1800):
            return None, "per_subagent_wallclock_seconds must be an int 30..1800"

    if merged["per_subagent_wallclock_seconds"] > merged["per_rca_wallclock_seconds"]:
        return None, "per_subagent_wallclock_seconds must be <= per_rca_wallclock_seconds"

    if "monthly_token_cap" in cleaned:
        v = cleaned["monthly_token_cap"]
        if v is not None and not _validate_int(v, 1_000_000, 2**63 - 1):
            return None, "monthly_token_cap must be null or an int >= 1000000"

    if "fallback_provider_chain" in cleaned:
        v = cleaned["fallback_provider_chain"]
        if not isinstance(v, list) or len(v) > 10:
            return None, "fallback_provider_chain must be a list of at most 10 strings"
        for item in v:
            if not isinstance(item, str) or not item.strip():
                return None, "fallback_provider_chain entries must be non-empty strings"

    return cleaned, None


def _select_row(cur, org_id: str):
    cur.execute(
        "SELECT max_parallel_subagents, max_total_subagents, max_delegate_depth, "
        "max_concurrent_rcas, multi_agent_min_severity, per_rca_token_budget, "
        "per_subagent_token_budget, per_rca_wallclock_seconds, "
        "per_subagent_wallclock_seconds, monthly_token_cap, "
        "fallback_provider_chain, updated_at "
        "FROM multi_agent_config WHERE org_id = %s",
        (org_id,),
    )
    return cur.fetchone()


@multi_agent_config_bp.route("/multi-agent-config", methods=["GET"])
@require_auth_only
def get_config(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[MultiAgentConfig:get]")
            row = _select_row(cur, org_id)

    if row is None:
        return jsonify({**_defaults(), "updated_at": None})
    return jsonify(_row_to_dict(row))


@multi_agent_config_bp.route("/multi-agent-config", methods=["PUT"])
@require_permission("admin", "access")
def update_config(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[MultiAgentConfig:update]")
            row = _select_row(cur, org_id)
            current = _row_to_dict(row) if row is not None else {**_defaults(), "updated_at": None}

            cleaned, err = _validate_payload(data, current)
            if err:
                return jsonify({"error": err}), 400

            merged = {k: current[k] for k in _COLUMNS}
            merged.update(cleaned)

            insert_cols = ["org_id"] + list(_COLUMNS)
            placeholders = ", ".join(["%s"] * len(insert_cols))
            insert_values = [org_id] + [merged[k] for k in _COLUMNS]

            update_cols = list(cleaned.keys()) if cleaned else []
            if update_cols:
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                set_clause += ", updated_at = NOW()"
            else:
                set_clause = "updated_at = NOW()"

            cur.execute(
                f"INSERT INTO multi_agent_config ({', '.join(insert_cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (org_id) DO UPDATE SET {set_clause}",
                insert_values,
            )

            row = _select_row(cur, org_id)
        conn.commit()

    return jsonify(_row_to_dict(row))


@multi_agent_config_bp.route("/multi-agent-config/reset", methods=["POST"])
@require_permission("admin", "access")
def reset_config(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[MultiAgentConfig:reset]")
            cur.execute(
                "DELETE FROM multi_agent_config WHERE org_id = %s",
                (org_id,),
            )
        conn.commit()

    return jsonify({"reset": True})

"""Aurora Watch settings (an org preference) and per-cluster detector state (aurora_watch_state).

``aurora_watch_state`` is FORCE-RLS by org, so every query here runs under
``set_rls_context`` for the acting user: these functions are called from Celery,
where no request sets the RLS variables.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

PREF_KEY = "aurora_watch"
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
MAX_NAMESPACES = 50
# "\u0000" in JSON text that encodes a NUL (an odd number of backslashes before "u0000").
_NUL_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\u0000")


def jsonb_dumps(value: Any) -> str:
    """JSON for a jsonb column. jsonb cannot hold U+0000, and cluster text (a log line,
    an event message) can contain it: it is dropped rather than failing the write."""
    return _NUL_ESCAPE.sub(r"\1", json.dumps(value, default=str))


def normalize_namespaces(value: Any) -> list[str] | None:
    """A clean namespace allowlist, or None for "every namespace except Kubernetes' own"."""
    if not value:
        return None
    if not isinstance(value, list):
        raise ValueError("namespaces must be a list of namespace names")
    names = sorted({str(n).strip() for n in value if str(n).strip()})
    bad = [n for n in names if not _NAMESPACE.match(n)]
    if bad:
        raise ValueError(f"invalid namespace name: {bad[0]}")
    if len(names) > MAX_NAMESPACES:
        raise ValueError(f"at most {MAX_NAMESPACES} namespaces")
    return names or None


def get_settings(org_id: str) -> dict[str, Any]:
    from utils.auth.stateless_auth import get_org_preference

    raw = get_org_preference(org_id, PREF_KEY, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled")),
        "owner_user_id": raw.get("owner_user_id"),
        "namespaces": raw.get("namespaces") or None,
        "updated_at": raw.get("updated_at"),
    }


def save_settings(org_id: str, *, enabled: bool, owner_user_id: str, namespaces: list[str] | None) -> dict[str, Any]:
    """Persist the org's Aurora Watch settings. ``owner_user_id`` is who Watch acts as."""
    from utils.auth.stateless_auth import store_org_preference

    value = {
        "enabled": bool(enabled),
        "owner_user_id": owner_user_id,
        "namespaces": namespaces,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    store_org_preference(org_id, PREF_KEY, value)
    return value


def list_clusters(user_id: str) -> list[dict[str, Any]]:
    """Kubernetes clusters the org has connected (in-cluster agent or kubeconfig)."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org_id = set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:clusters]")
        if not org_id:
            return []
        cur.execute(
            """
            SELECT c.cluster_id, t.cluster_name, 'agent' AS source, c.connected_at AT TIME ZONE 'UTC'
              FROM active_kubectl_connections c
              JOIN kubectl_agent_tokens t ON c.token = t.token
             WHERE t.org_id = %s AND c.status = 'active'
            UNION
            SELECT cluster_id, cluster_name, 'kubeconfig' AS source, created_at
              FROM kubeconfig_clusters
             WHERE org_id = %s AND is_active = TRUE
            """,
            (org_id, org_id),
        )
        rows = cur.fetchall()
    return [{"cluster_id": r[0], "name": r[1], "source": r[2],
             "connected_at": r[3].isoformat() if r[3] else None} for r in rows if r[0]]


def load_state(user_id: str, cluster_id: str) -> dict[str, Any] | None:
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org_id = set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:load]")
        if not org_id:
            return None
        cur.execute(
            "SELECT state FROM aurora_watch_state WHERE org_id = %s AND cluster_id = %s",
            (org_id, cluster_id),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    state = row[0]
    return json.loads(state) if isinstance(state, str) else state


def save_state(user_id: str, cluster_id: str, state: dict[str, Any] | None, *,
               blind_spots: list[str] | None = None, error: str | None = None) -> None:
    """Upsert the detector state and the last poll's outcome. ``state=None`` keeps the stored state."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    summary = {
        "polls": (state or {}).get("polls", 0),
        "open": len((state or {}).get("open") or {}),
    }
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org_id = set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:save]")
        if not org_id:
            return
        if state is None:
            cur.execute(
                """
                INSERT INTO aurora_watch_state (org_id, cluster_id, state, last_error, blind_spots, last_poll_at, updated_at)
                VALUES (%s, %s, '{}'::jsonb, %s, %s, NOW(), NOW())
                ON CONFLICT (org_id, cluster_id) DO UPDATE
                   SET last_error = EXCLUDED.last_error, blind_spots = EXCLUDED.blind_spots,
                       last_poll_at = NOW(), updated_at = NOW()
                """,
                (org_id, cluster_id, error, jsonb_dumps(blind_spots or [])),
            )
        else:
            cur.execute(
                """
                INSERT INTO aurora_watch_state
                    (org_id, cluster_id, state, polls, open_findings, last_error, blind_spots, last_poll_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (org_id, cluster_id) DO UPDATE
                   SET state = EXCLUDED.state, polls = EXCLUDED.polls, open_findings = EXCLUDED.open_findings,
                       last_error = EXCLUDED.last_error, blind_spots = EXCLUDED.blind_spots,
                       last_poll_at = NOW(), updated_at = NOW()
                """,
                (org_id, cluster_id, jsonb_dumps(state), summary["polls"], summary["open"],
                 error, jsonb_dumps(blind_spots or [])),
            )
        conn.commit()


def status(user_id: str) -> list[dict[str, Any]]:
    """Per-cluster watch status for the settings page: learning, open findings, last poll."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org_id = set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:status]")
        if not org_id:
            return []
        cur.execute(
            """
            SELECT cluster_id, state, polls, last_poll_at, last_error, blind_spots
              FROM aurora_watch_state
             WHERE org_id = %s
             ORDER BY cluster_id
            """,
            (org_id,),
        )
        rows = cur.fetchall()
    out = []
    for cluster_id, state, polls, last_poll_at, last_error, blind_spots in rows:
        state = (json.loads(state) if isinstance(state, str) else state) or {}
        config = state.get("config") or {}
        out.append({
            "cluster_id": cluster_id,
            "polls": polls or 0,
            "learning": (polls or 0) < config.get("learning_polls", 5),
            "last_poll_at": last_poll_at.isoformat() if last_poll_at else None,
            "seconds_since_poll": (datetime.now(timezone.utc) - last_poll_at).total_seconds() if last_poll_at else None,
            "last_error": last_error,
            "blind_spots": (json.loads(blind_spots) if isinstance(blind_spots, str) else blind_spots) or [],
            "open_findings": [
                {"subject": f.get("subject"), "severity": f.get("severity"), "summary": f.get("summary")}
                for f in (state.get("open") or {}).values()
            ],
        })
    return out

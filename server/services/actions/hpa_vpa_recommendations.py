"""Lifecycle helpers for HPA/VPA right-sizing recommendations.

Owns every read and write of ``hpa_vpa_recommendations`` plus the PR-close
dispatch, so the two callers that live in different containers share one set of
invariants:

- the card tool (``celery_worker`` / ``chatbot``) claims a workload and posts
- the Slack Dismiss handler (``aurora-server``) transitions it and closes the PR

No raw SQL against this table belongs anywhere else. Every function fails soft
and returns ``{"error": ...}`` rather than raising -- ``initialize_tables()``
runs at boot in ``aurora-server`` only, so a worker that starts first can
legitimately see a missing table and must not crash the agent loop.
"""

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# Days a dismissed workload stays suppressed. Module constant so tests can
# monkeypatch it and so tuning it later does not mean re-deriving cooldowns
# from dismissed_at -- cooldown_until is stamped absolute at dismiss time.
HPA_VPA_COOLDOWN_DAYS = 30

# A re-proposal during cooldown must clear this multiple of the dismissed
# severity to count as "materially worse". This is the only thing that makes
# the design doc's "unless the mis-size materially worsens" computable rather
# than an LLM judgement call.
MATERIALLY_WORSE_FACTOR = 1.25

# Terminal/live status values -- see the DDL comment in db_utils.py.
STATUS_PROPOSED = "proposed"
STATUS_DISMISSED = "dismissed"
STATUS_MERGED = "merged"
STATUS_CLOSED = "closed"
STATUS_SUPERSEDED = "superseded"

_ROW_FIELDS = (
    "id", "workload_key", "workload", "environment", "service", "autoscaler",
    "metrics_source", "vcs_provider", "repo_full_name", "pr_number", "pr_url",
    "status", "recommendation", "severity_score", "slack_channel_id",
    "slack_message_ts", "dismissed_by", "dismissed_at", "cooldown_until",
    "created_at", "updated_at",
)


# ---------------------------------------------------------------------------
# Keys and scoring
# ---------------------------------------------------------------------------


def build_workload_key(workload: str, environment: Optional[str] = None) -> str:
    """Normalized dedup/cooldown key. Computed here, never accepted from the LLM.

    The same workload in two environments is two independent rows, and
    case/whitespace drift in the model's output cannot defeat a cooldown.
    """
    env = (environment or "").strip() or "-"
    return f"{env}::{(workload or '').strip()}".lower()


def _advisory_lock_key(org_id: str, workload_key: str) -> int:
    """Stable bigint lock key for pg_advisory_xact_lock (executor.py pattern)."""
    digest = hashlib.sha256(f"{org_id}:{workload_key}".encode()).digest()[:7]
    return int.from_bytes(digest, byteorder="big", signed=False) & 0x7FFFFFFFFFFFFFFF


def compute_severity_score(dimensions: dict) -> Optional[float]:
    """Max relative mis-size across dimensions: max(abs(current - rec) / current).

    ``dimensions`` maps a dimension name to a dict with numeric ``current`` and
    ``recommended``. Dimensions missing either value, or with a non-positive
    current, are skipped. Returns None when nothing is scoreable.
    """
    scores = []
    for spec in (dimensions or {}).values():
        if not isinstance(spec, dict):
            continue
        current, recommended = spec.get("current"), spec.get("recommended")
        if not isinstance(current, (int, float)) or not isinstance(recommended, (int, float)):
            continue
        if current <= 0:
            continue
        scores.append(abs(float(recommended) - float(current)) / float(current))
    return max(scores) if scores else None


def is_materially_worse(new_score: Optional[float], prior_score: Optional[float]) -> bool:
    """Whether a new recommendation justifies breaking an active cooldown.

    Unknown scores are treated as NOT worse: a missing number must never be a
    reason to nag someone who already said no. That includes NaN and infinity --
    the new score originates from the LLM, and `inf` would otherwise compare as
    worse than everything and break any cooldown on demand. Callers sanitize too;
    this is the gate, so it enforces the invariant itself.
    """
    if isinstance(new_score, bool) or not isinstance(new_score, (int, float)):
        return False
    if isinstance(prior_score, bool) or not isinstance(prior_score, (int, float)):
        return False
    if not math.isfinite(new_score) or not math.isfinite(prior_score):
        return False
    if prior_score <= 0:
        return False
    return float(new_score) >= float(prior_score) * MATERIALLY_WORSE_FACTOR


def _row_to_dict(row: tuple) -> dict:
    """Map a _ROW_FIELDS-ordered tuple to a JSON-safe dict."""
    out: dict[str, Any] = {}
    for key, value in zip(_ROW_FIELDS, row):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif key == "severity_score" and value is not None:
            out[key] = float(value)
        elif key == "id":
            out[key] = str(value)
        else:
            out[key] = value
    return out


_SELECT_ROW = f"SELECT {', '.join(_ROW_FIELDS)} FROM hpa_vpa_recommendations"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_active_cooldown(cursor, org_id: str, workload_key: str) -> Optional[dict]:
    """Most recent dismissal whose cooldown is still running, or None."""
    cursor.execute(
        f"""{_SELECT_ROW}
             WHERE org_id = %s AND workload_key = %s
               AND status = %s AND cooldown_until > NOW()
             ORDER BY dismissed_at DESC LIMIT 1""",
        (org_id, workload_key, STATUS_DISMISSED),
    )
    row = cursor.fetchone()
    return _row_to_dict(row) if row else None


def get_live_recommendation(cursor, org_id: str, workload_key: str) -> Optional[dict]:
    """The single open ('proposed') recommendation for a workload, or None."""
    cursor.execute(
        f"{_SELECT_ROW} WHERE org_id = %s AND workload_key = %s AND status = %s LIMIT 1",
        (org_id, workload_key, STATUS_PROPOSED),
    )
    row = cursor.fetchone()
    return _row_to_dict(row) if row else None


def list_recommendations(user_id: str) -> dict:
    """Live proposals plus workloads still inside a cooldown window.

    This is what lets the prompt check prior work *before* opening a PR. Doing
    it after is the worst ordering: a suppressed workload still gets a PR
    nobody asked for.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, user_id, log_prefix="[HpaVpaRecs]")
                if not org_id:
                    return {"error": "Could not resolve organization context"}
                cur.execute(
                    f"""{_SELECT_ROW}
                         WHERE org_id = %s
                           AND (status = %s
                                OR (status = %s AND cooldown_until > NOW()))
                         ORDER BY created_at DESC LIMIT 200""",
                    (org_id, STATUS_PROPOSED, STATUS_DISMISSED),
                )
                rows = [_row_to_dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("[HpaVpaRecs] Failed to list recommendations for user=%s", sanitize(user_id))
        return {"error": "Could not read existing right-sizing recommendations"}

    open_recs = [r for r in rows if r["status"] == STATUS_PROPOSED]
    cooling = [r for r in rows if r["status"] == STATUS_DISMISSED]
    return {
        "open_recommendations": open_recs,
        "in_cooldown": cooling,
        "counts": {"open": len(open_recs), "in_cooldown": len(cooling)},
        "cooldown_days": HPA_VPA_COOLDOWN_DAYS,
        "guidance": (
            "Update an open recommendation instead of opening a second PR for the same "
            "workload. Skip a workload in cooldown unless the mis-size has materially "
            f"worsened (>= {MATERIALLY_WORSE_FACTOR}x the dismissed severity)."
        ),
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def lock_workload(cursor, org_id: str, workload_key: str) -> None:
    """Serialize concurrent dedup for one (org, workload) inside this txn.

    SELECT-then-INSERT under an advisory lock, not ON CONFLICT against the
    partial unique index -- Postgres only infers a partial index when the
    statement repeats its predicate, which is fiddly and easy to get subtly
    wrong. The unique index stays as the backstop.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_advisory_lock_key(org_id, workload_key),))


def claim_recommendation(
    cursor, org_id: str, user_id: str, *, workload_key: str, workload: str,
    environment: Optional[str], service: Optional[str], autoscaler: Optional[str],
    metrics_source: Optional[str], vcs_provider: str, repo_full_name: str,
    pr_number: int, pr_url: str, recommendation: dict,
    severity_score: Optional[float], action_run_id: Optional[str] = None,
) -> str:
    """INSERT a 'proposed' row with a NULL message ts and return its UUID.

    Called under :func:`lock_workload` and *before* the Slack post, so the
    workload is claimed and the UUID the Dismiss button needs exists before any
    external call can fail halfway.
    """
    cursor.execute(
        """INSERT INTO hpa_vpa_recommendations
             (org_id, user_id, workload_key, workload, environment, service, autoscaler,
              metrics_source, vcs_provider, repo_full_name, pr_number, pr_url,
              status, recommendation, severity_score, action_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
           RETURNING id""",
        (org_id, user_id, workload_key, workload, environment, service, autoscaler,
         metrics_source, vcs_provider, repo_full_name, pr_number, pr_url,
         STATUS_PROPOSED, json.dumps(recommendation or {}), severity_score, action_run_id),
    )
    return str(cursor.fetchone()[0])


def attach_slack_message(cursor, rec_id: str, channel_id: str, message_ts: str) -> None:
    """Record where the card landed, so it can be updated and rewritten later."""
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET slack_channel_id = %s, slack_message_ts = %s, updated_at = NOW()
            WHERE id = %s::uuid""",
        (channel_id, message_ts, rec_id),
    )


def delete_recommendation(cursor, rec_id: str) -> None:
    """Release a claimed row after a failed post, so a transient Slack outage
    does not permanently block the workload."""
    cursor.execute("DELETE FROM hpa_vpa_recommendations WHERE id = %s::uuid", (rec_id,))


def refresh_recommendation(
    cursor, rec_id: str, *, recommendation: dict, severity_score: Optional[float],
    repo_full_name: str, pr_number: int, pr_url: str,
    autoscaler: Optional[str] = None, metrics_source: Optional[str] = None,
) -> None:
    """Update an existing open recommendation with fresh numbers (card dedup path)."""
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET recommendation = %s::jsonb, severity_score = %s, repo_full_name = %s,
                  pr_number = %s, pr_url = %s,
                  autoscaler = COALESCE(%s, autoscaler),
                  metrics_source = COALESCE(%s, metrics_source),
                  updated_at = NOW()
            WHERE id = %s::uuid""",
        (json.dumps(recommendation or {}), severity_score, repo_full_name,
         pr_number, pr_url, autoscaler, metrics_source, rec_id),
    )


def mark_superseded(cursor, rec_id: str) -> None:
    """Retire a row whose mis-size worsened, or that a newer rec took over."""
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET status = %s, cooldown_until = NULL, updated_at = NOW()
            WHERE id = %s::uuid""",
        (STATUS_SUPERSEDED, rec_id),
    )


def dismiss_recommendation(cursor, rec_id: str, org_id: str, slack_user_id: str) -> Optional[dict]:
    """Atomically transition 'proposed' -> 'dismissed' and start the cooldown.

    The ``status = 'proposed'`` predicate *is* the double-click defence: a
    second click matches zero rows. Returns the row's details on the winning
    transition, or None when it had already been dismissed.
    """
    now = datetime.now(timezone.utc)
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET status = %s, dismissed_by = %s, dismissed_at = %s,
                  cooldown_until = %s, updated_at = %s
            WHERE id = %s::uuid AND org_id = %s AND status = %s
          RETURNING repo_full_name, pr_number, pr_url, workload, environment, service,
                    autoscaler, vcs_provider, recommendation, severity_score,
                    slack_channel_id, slack_message_ts, user_id""",
        (STATUS_DISMISSED, slack_user_id, now,
         now + timedelta(days=HPA_VPA_COOLDOWN_DAYS), now,
         rec_id, org_id, STATUS_PROPOSED),
    )
    row = cursor.fetchone()
    if not row:
        return None
    # user_id is the account whose GitHub credential opened the PR. The clicker
    # is a different person and may have no GitHub connection at all, so the
    # close must be able to fall back to the opener.
    keys = ("repo_full_name", "pr_number", "pr_url", "workload", "environment", "service",
            "autoscaler", "vcs_provider", "recommendation", "severity_score",
            "slack_channel_id", "slack_message_ts", "user_id")
    out = dict(zip(keys, row))
    out["cooldown_until"] = (now + timedelta(days=HPA_VPA_COOLDOWN_DAYS)).isoformat()
    if out.get("severity_score") is not None:
        out["severity_score"] = float(out["severity_score"])
    return out


def mark_merged(cursor, rec_id: str, org_id: str) -> None:
    """A merged PR was *accepted*, not rejected.

    Clearing cooldown_until matters: a merge must never start an anti-nag
    window, or the next genuine drift on this workload goes unreported.
    """
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET status = %s, cooldown_until = NULL, updated_at = NOW()
            WHERE id = %s::uuid AND org_id = %s""",
        (STATUS_MERGED, rec_id, org_id),
    )


# ---------------------------------------------------------------------------
# PR close -- provider dispatch
# ---------------------------------------------------------------------------


def _close_github_pr(user_id: str, repo_full_name: str, pr_number: int, timeout: int) -> dict:
    """PATCH /repos/{repo}/pulls/{n} {"state": "closed"}.

    First ``requests.patch`` in ``server/`` -- every other GitHub call here is
    read-only (``_call_github_api`` hardcodes ``requests.get``). Not routed
    through MCP on purpose: ``mcp_github_update_pull_request`` is allowlisted in
    ``mcp_tools.py`` but absent from ``tool_registry.py``, so ``gate_action``
    denies it in background context. Direct REST is the only reliable path.

    Uses ``get_auth_for_user_repo`` rather than ``get_installation_token``: we
    know ``(user_id, repo_full_name)`` and not the installation id, and the
    router handles App-vs-OAuth and sets its own RLS context (Celery-safe).
    """
    from utils.auth.github_auth_router import (
        NoGitHubAuthError,
        get_auth_for_user_repo,
        make_auth_header,
    )

    try:
        auth = get_auth_for_user_repo(user_id, repo_full_name)
    except NoGitHubAuthError as exc:
        return {"error": f"No GitHub credential for {repo_full_name}: {exc}"}
    except Exception as exc:
        logger.exception("[HpaVpaRecs] GitHub auth resolution failed for user=%s", sanitize(user_id))
        return {"error": f"GitHub auth resolution failed: {type(exc).__name__}"}

    headers = {
        **make_auth_header(auth),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"

    try:
        resp = requests.patch(url, headers=headers, json={"state": "closed"}, timeout=timeout)
    except requests.RequestException as exc:
        return {"error": f"GitHub API request failed: {type(exc).__name__}"}

    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        # A merged PR means the human already accepted. GitHub reports the
        # merge rather than erroring, and treating it as a rejection would
        # start an anti-nag window on an *approved* change.
        if body.get("merged"):
            return {"success": True, "already_merged": True, "state": body.get("state")}
        return {"success": True, "state": body.get("state", "closed")}
    if resp.status_code == 404:
        # Already gone, or no access. Idempotent either way -- there is no open
        # PR left to close, which is the state the caller wanted.
        return {"success": True, "already_gone": True}
    if resp.status_code == 403:
        return {"error": f"GitHub returned 403 closing PR #{pr_number} (rate limit or missing permission)"}
    if resp.status_code == 422:
        return {"error": f"GitHub returned 422 closing PR #{pr_number} (invalid state): {resp.text[:200]}"}
    return {"error": f"GitHub API status={resp.status_code} closing PR #{pr_number}: {resp.text[:200]}"}


# Only GitHub is implemented today. The other two are a fill-in, not a schema
# migration, which is what the vcs_provider column buys us:
#   "gitlab"    -> PUT /projects/{id}/merge_requests/{iid} {"state_event": "close"}
#                  via gitlab_api_request (verb-generic: requests.request(method, ...))
#   "bitbucket" -> client.decline_pull_request(...)  (already exists)
_CLOSERS = {"github": _close_github_pr}


def close_pull_request(
    user_id: str, vcs_provider: str, repo_full_name: str, pr_number: int, timeout: int = 30
) -> dict:
    """Close an open PR/MR on the provider that hosts it.

    Returns ``{"success": True, ...}`` or ``{"error": ...}``. An unknown
    provider is a loud error rather than a silent no-op -- same discipline as
    the trailing ``raise AssertionError`` in ``_do_query_logs``.

    A blank provider is an error too, not a silent default to GitHub: callers
    already supply the fallback, so a blank here means the row is malformed and
    guessing would fire a real GitHub PATCH at whatever repo string came with it.
    """
    provider = (vcs_provider or "").lower().strip()
    closer = _CLOSERS.get(provider)
    if closer is None:
        return {"error": (f"Cannot close PR: unsupported vcs_provider '{provider}'. "
                          f"Supported: {', '.join(sorted(_CLOSERS))}")}
    if not repo_full_name or not pr_number:
        return {"error": "Cannot close PR: missing repository or PR number"}

    logger.info(
        "[HpaVpaRecs] Closing %s PR #%s in %s for user=%s",
        provider, pr_number, sanitize(repo_full_name), sanitize(user_id),
    )
    return closer(user_id, repo_full_name, int(pr_number), timeout)

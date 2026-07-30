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
import re
from dataclasses import dataclass
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

# Exactly owner/repo, the only shape any supported provider accepts. Guards the
# API URL these values are interpolated into.
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

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


def _is_real_number(value: object) -> bool:
    """Whether a value is a finite int/float usable in arithmetic.

    Rejects bool (an int subclass, so True would score as 1) and NaN/inf, both of
    which reach us from LLM-supplied JSON and would otherwise corrupt a
    comparison rather than fail it.

    ``math.isfinite`` raises OverflowError on an arbitrary-precision int too
    large for a float (``json.loads`` imposes no bound, so a model can emit one),
    which would propagate out of the cooldown gate as an unhandled error rather
    than a decision. Treat unrepresentable as not usable.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _dimension_score(spec: object) -> Optional[float]:
    """Relative mis-size for one dimension, or None if it is not scoreable."""
    if not isinstance(spec, dict):
        return None
    current, recommended = spec.get("current"), spec.get("recommended")
    if not (_is_real_number(current) and _is_real_number(recommended)):
        return None
    if current <= 0:
        return None
    return abs(float(recommended) - float(current)) / float(current)


def compute_severity_score(dimensions: dict) -> Optional[float]:
    """Max relative mis-size across dimensions: max(abs(current - rec) / current).

    ``dimensions`` maps a dimension name to a dict with numeric ``current`` and
    ``recommended``. Dimensions missing either value, or with a non-positive
    current, are skipped. Returns None when nothing is scoreable.
    """
    scores = [s for s in map(_dimension_score, (dimensions or {}).values()) if s is not None]
    return max(scores) if scores else None


# Kubernetes quantity suffixes, normalized to a common base per dimension. Only
# ratios within one dimension are ever taken, so the absolute base is irrelevant
# as long as it is consistent.
_QUANTITY_UNITS = {
    "": 1.0,
    "m": 0.001,                      # millicores
    "k": 1e3, "ki": 1024.0,
    "M": 1e6, "mi": 1024.0 ** 2,
    "G": 1e9, "gi": 1024.0 ** 3,
    "T": 1e12, "ti": 1024.0 ** 4,
    "P": 1e15, "pi": 1024.0 ** 5,
}
_QUANTITY_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")

# Which magnitude family a suffix belongs to. A ratio is only meaningful between
# two quantities in the SAME family: '512m' vs '256Mi' is a units mistake, not a
# 5e8x mis-size, and scoring it would hand the model a severity high enough to
# break any cooldown it likes.
#
# Bare numbers and 'm' share a family on purpose -- Kubernetes treats `cpu: 2`
# and `cpu: 2000m` as the same quantity, and a replica count is bare too. The
# split that matters is scalar/milli vs the byte suffixes, which is where the
# 1e9x mistakes live.
_QUANTITY_FAMILIES = {
    "": "count",
    "m": "count",
    "k": "bytes", "ki": "bytes",
    "M": "bytes", "mi": "bytes",
    "G": "bytes", "gi": "bytes",
    "T": "bytes", "ti": "bytes",
    "P": "bytes", "pi": "bytes",
}


def _quantity_family(suffix: str) -> Optional[str]:
    """Magnitude family for a matched suffix, or None when unrecognized."""
    family = _QUANTITY_FAMILIES.get(suffix)
    if family is None and len(suffix) == 2:
        family = _QUANTITY_FAMILIES.get(suffix.lower())
    return family


def _split_quantity(text: object) -> Optional[tuple]:
    """Parse a display quantity into ``(value, family)``, or None.

    ``family`` is what makes a cross-unit comparison detectable: see
    :func:`severity_from_display`.
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return (float(text), "count") if _is_real_number(text) else None
    if not isinstance(text, str):
        return None

    match = _QUANTITY_RE.match(text)
    if not match:
        return None
    number, suffix = match.group(1), match.group(2)

    scale = _QUANTITY_UNITS.get(suffix)
    if scale is None:
        # Binary suffixes vary in case in the wild ('Gi', 'GI', 'gi'); the
        # single-letter decimal ones do not, since m vs M is milli vs mega.
        scale = _QUANTITY_UNITS.get(suffix.lower()) if len(suffix) == 2 else None
    if scale is None:
        return None

    try:
        value = float(number) * scale
    except (TypeError, ValueError, OverflowError):
        return None
    if not _is_real_number(value):
        return None
    return value, _quantity_family(suffix)


def parse_quantity(text: object) -> Optional[float]:
    """Parse a Kubernetes-style display quantity ('2 Gi', '750m', '10') to a float.

    Case matters where Kubernetes says it does: ``m`` is milli and ``M`` is mega,
    so ``750m`` and ``750M`` must not collapse to the same number. Binary
    suffixes (``Ki``/``Mi``/``Gi``) are matched case-insensitively because that is
    where real-world YAML actually varies.

    Returns None for anything unparseable, which the caller treats as "unknown"
    and therefore never as grounds to break a cooldown.
    """
    parsed = _split_quantity(text)
    return parsed[0] if parsed else None


def severity_from_display(payload: dict) -> Optional[float]:
    """Compute severity from the same display strings that go on the card.

    The alternative -- trusting an LLM-supplied ``severity_score`` -- puts the
    model in charge of whether it may break a human's 30-day cooldown, while also
    being the party that reports how bad the problem is. Deriving the number from
    the current/recommended values it already has to state keeps the two
    consistent and takes the judgement call away from the model.

    A dimension whose two values are in different magnitude families ('512m' vs
    '256Mi') is skipped rather than scored: the ratio would be astronomical and
    would break any cooldown, when the real defect is a units mistake in the
    model's own display strings.

    Returns None when nothing parses, which never breaks a cooldown.
    """
    numeric = {}
    for dimension, spec in (payload or {}).items():
        if not isinstance(spec, dict):
            continue
        current = _split_quantity(spec.get("current"))
        recommended = _split_quantity(spec.get("recommended"))
        if not (current and recommended):
            continue
        if current[1] != recommended[1]:
            logger.warning(
                "[HpaVpaRecs] Mismatched units on %s (%r -> %r); not scoring this "
                "dimension, so it cannot break a cooldown",
                dimension, spec.get("current"), spec.get("recommended"),
            )
            continue
        numeric[dimension] = {"current": current[0], "recommended": recommended[0]}
    return compute_severity_score(numeric)


def is_materially_worse(new_score: Optional[float], prior_score: Optional[float]) -> bool:
    """Whether a new recommendation justifies breaking an active cooldown.

    Unknown scores are treated as NOT worse: a missing number must never be a
    reason to nag someone who already said no. That includes NaN and infinity --
    the new score originates from the LLM, and `inf` would otherwise compare as
    worse than everything and break any cooldown on demand. Callers sanitize too;
    this is the gate, so it enforces the invariant itself.
    """
    if not (_is_real_number(new_score) and _is_real_number(prior_score)):
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


@dataclass(frozen=True)
class WorkloadRecommendation:
    """The per-workload facts a claim needs, as one value.

    Grouped rather than passed as 16 separate keyword arguments: the fields
    travel together everywhere, and a long keyword list is exactly where a
    ``service``/``environment`` transposition hides.
    """

    workload_key: str
    workload: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    recommendation: dict
    environment: Optional[str] = None
    service: Optional[str] = None
    autoscaler: Optional[str] = None
    metrics_source: Optional[str] = None
    vcs_provider: str = "github"
    severity_score: Optional[float] = None
    action_run_id: Optional[str] = None


def claim_recommendation(cursor, org_id: str, user_id: str, rec: WorkloadRecommendation) -> str:
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
        (org_id, user_id, rec.workload_key, rec.workload, rec.environment, rec.service,
         rec.autoscaler, rec.metrics_source, rec.vcs_provider, rec.repo_full_name,
         rec.pr_number, rec.pr_url, STATUS_PROPOSED, json.dumps(rec.recommendation or {}),
         rec.severity_score, rec.action_run_id),
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
    repo_full_name: str, pr_number: int, pr_url: str, vcs_provider: str,
    autoscaler: Optional[str] = None, metrics_source: Optional[str] = None,
) -> None:
    """Update an existing open recommendation with fresh numbers (card dedup path).

    ``vcs_provider`` is written alongside the PR reference on purpose. The three
    identify one PR together, so updating the repo and number while leaving a
    stale provider behind points Dismiss at the wrong API: it would send a GitHub
    PATCH at a GitLab MR number, or refuse to close a PR it could have closed.
    Only ``github`` is supported today, so this cannot bite yet -- which is
    exactly why it is worth fixing before a second provider makes it a live bug.
    """
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET recommendation = %s::jsonb, severity_score = %s, repo_full_name = %s,
                  pr_number = %s, pr_url = %s, vcs_provider = %s,
                  autoscaler = COALESCE(%s, autoscaler),
                  metrics_source = COALESCE(%s, metrics_source),
                  updated_at = NOW()
            WHERE id = %s::uuid""",
        (json.dumps(recommendation or {}), severity_score, repo_full_name,
         pr_number, pr_url, vcs_provider, autoscaler, metrics_source, rec_id),
    )


def mark_superseded(cursor, rec_id: str) -> None:
    """Retire a row whose mis-size worsened, or that a newer rec took over.

    ``cooldown_until`` is deliberately left intact rather than nulled. Nulling it
    is invisible to every read path (``get_active_cooldown`` and
    ``list_recommendations`` both filter on ``status = 'dismissed'``), but it
    destroys the only record of how much anti-nag window was left -- and this
    transition is committed *before* the Slack post that justifies it, so a post
    failure has to be able to put the dismissal back. See
    :func:`restore_superseded`.
    """
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET status = %s, updated_at = NOW()
            WHERE id = %s::uuid""",
        (STATUS_SUPERSEDED, rec_id),
    )


def restore_superseded(cursor, rec_id: str) -> bool:
    """Undo :func:`mark_superseded`, restoring the dismissal and its cooldown.

    Compensation for a superseding card that never actually posted. Without it
    the human's remaining anti-nag window is gone for good: the dismissal was
    retired to make room for a card that does not exist, so the next run sees no
    cooldown and re-proposes a workload a human already rejected.

    Only touches rows still in 'superseded', so it cannot resurrect a dismissal
    that a later, genuinely-posted recommendation retired.
    """
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET status = %s, updated_at = NOW()
            WHERE id = %s::uuid AND status = %s""",
        (STATUS_DISMISSED, rec_id, STATUS_SUPERSEDED),
    )
    return cursor.rowcount > 0


# Fields dismiss_recommendation returns. Single definition: the SQL RETURNING
# list is generated from it, so the tuple and the mapping can never drift.
# user_id is the account whose GitHub credential opened the PR -- the clicker is
# a different person who may have no GitHub connection at all, so the close must
# be able to fall back to the opener.
_DISMISS_FIELDS = (
    "repo_full_name", "pr_number", "pr_url", "workload", "environment", "service",
    "autoscaler", "vcs_provider", "recommendation", "severity_score",
    "slack_channel_id", "slack_message_ts", "user_id",
)


def dismiss_recommendation(cursor, rec_id: str, org_id: str, slack_user_id: str) -> Optional[dict]:
    """Atomically transition 'proposed' -> 'dismissed' and start the cooldown.

    The ``status = 'proposed'`` predicate *is* the double-click defence: a
    second click matches zero rows. Returns the row's details on the winning
    transition, or None when it had already been dismissed.
    """
    now = datetime.now(timezone.utc)
    cooldown_until = now + timedelta(days=HPA_VPA_COOLDOWN_DAYS)
    cursor.execute(
        f"""UPDATE hpa_vpa_recommendations
               SET status = %s, dismissed_by = %s, dismissed_at = %s,
                   cooldown_until = %s, updated_at = %s
             WHERE id = %s::uuid AND org_id = %s AND status = %s
           RETURNING {', '.join(_DISMISS_FIELDS)}""",
        (STATUS_DISMISSED, slack_user_id, now, cooldown_until, now,
         rec_id, org_id, STATUS_PROPOSED),
    )
    row = cursor.fetchone()
    if not row:
        return None
    # strict=True: a silently truncated zip would mis-key every field after the
    # mismatch, and the caller uses these to close a real PR.
    out = dict(zip(_DISMISS_FIELDS, row, strict=True))
    out["cooldown_until"] = cooldown_until.isoformat()
    if out.get("severity_score") is not None:
        out["severity_score"] = float(out["severity_score"])
    return out


def mark_merged(cursor, rec_id: str, org_id: str) -> bool:
    """A merged PR was *accepted*, not rejected.

    Clearing cooldown_until matters: a merge must never start an anti-nag
    window, or the next genuine drift on this workload goes unreported.

    Returns whether a row was actually updated. A no-op means the cooldown is
    still running on an *accepted* change, which the caller has to report rather
    than claim the intended outcome.
    """
    cursor.execute(
        """UPDATE hpa_vpa_recommendations
              SET status = %s, cooldown_until = NULL, updated_at = NOW()
            WHERE id = %s::uuid AND org_id = %s""",
        (STATUS_MERGED, rec_id, org_id),
    )
    return cursor.rowcount > 0


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
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{int(pr_number)}"

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
        # GitHub returns 404 for both "no such PR" and "no access to this repo",
        # deliberately, so the two are indistinguishable from here. Still treated
        # as success, because retrying cannot help and the dismissal must not be
        # rolled back over it -- but flagged as unverified so the caller can say
        # "could not be confirmed" rather than "closed". Claiming a clean close
        # we never observed is the one outcome worth avoiding: it tells a human
        # to stop looking at a PR that may well still be open.
        logger.warning(
            "[HpaVpaRecs] GitHub 404 closing PR #%s in %s -- already closed, or the "
            "credential cannot see the repo. Treating as closed but unverified.",
            pr_number, sanitize(repo_full_name),
        )
        return {"success": True, "already_gone": True, "unverified": True,
                "detail": ("GitHub returned 404: the PR is already gone, or this credential "
                           "cannot see the repository. Close state could not be confirmed.")}
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


def supported_vcs_providers() -> frozenset:
    """Providers a PR can actually be closed on.

    Callers validate against this *before* posting a card, so a workload is
    never proposed on a provider whose Dismiss could not close the PR.
    """
    return frozenset(_CLOSERS)


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
    # The repo slug is interpolated into an API URL, so constrain it to exactly
    # owner/repo. Rejects traversal ("../.."), extra path segments and query or
    # fragment characters that would otherwise retarget the request.
    if not _REPO_FULL_NAME_RE.match(repo_full_name):
        return {"error": ("Cannot close PR: malformed repository name "
                          f"'{repo_full_name[:60]}' (expected 'owner/repo')")}
    try:
        pr_number = int(pr_number)
    except (TypeError, ValueError):
        return {"error": f"Cannot close PR: PR number '{pr_number}' is not an integer"}
    if pr_number <= 0:
        return {"error": f"Cannot close PR: PR number {pr_number} is not positive"}

    logger.info(
        "[HpaVpaRecs] Closing %s PR #%s in %s for user=%s",
        provider, pr_number, sanitize(repo_full_name), sanitize(user_id),
    )
    return closer(user_id, repo_full_name, pr_number, timeout)

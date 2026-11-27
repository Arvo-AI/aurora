"""RCA model routing: route each alert-triggered RCA to a cheap or main model.

Before the background investigation starts, the incident is classified with a
few cheap SQL lookups (no LLM call):

* ``repeat`` — a prior incident with the same alert already has a completed
  RCA that ran on the *main* model. Evidence is either the rule correlator's
  ``correlation_hint`` (score >= RCA_ROUTING_REPEAT_MIN_SCORE) or an exact
  (source, title, service) match inside RCA_ROUTING_REPEAT_LOOKBACK_HOURS.
* ``simple`` — severity is in RCA_ROUTING_SIMPLE_SEVERITIES (default ``low``).
* ``hard`` — everything else, including unknown severity and any incident whose
  severity is in RCA_ROUTING_MAIN_SEVERITIES (default ``critical``), which is
  never routed to the cheap model.

repeat/simple use RCA_ROUTING_CHEAP_MODEL; hard uses the normal RCA model.
When anything is uncertain (lookup error, missing cheap model, orchestrator
path, unknown mode) the main model is used.

Modes (RCA_MODEL_ROUTING_MODE): ``off`` (default, nothing runs), ``shadow``
(classify and record, always use the main model), ``live`` (route).

The decision is written to ``incidents.rca_route`` / ``rca_model_tier`` /
``rca_model`` / ``rca_route_reason`` so token cost can be grouped per route by
joining ``llm_usage_tracking.session_id`` to ``incidents.aurora_chat_session_id``.
"""

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[RCA_ROUTING]"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)

ROUTE_REPEAT = "repeat"
ROUTE_SIMPLE = "simple"
ROUTE_HARD = "hard"

TIER_CHEAP = "cheap"
TIER_MAIN = "main"

# Sources that are follow-ups or non-alert work, never routed: the user is
# talking to the agent, or the run is an action / postmortem / PR review.
_NON_ALERT_SOURCES = frozenset({
    "action", "chat", "slack", "teams", "google_chat",
    "postmortem_generation", "change_gating",
})

_DEFAULT_SIMPLE_SEVERITIES = "low"
_DEFAULT_MAIN_SEVERITIES = "critical"
_DEFAULT_LOOKBACK_HOURS = 168.0
_MAX_LOOKBACK_HOURS = 24.0 * 90
_DEFAULT_MIN_SCORE = 0.75

_REASON_MAX_LEN = 500
_PRIOR_SUMMARY_MAX_CHARS = 1500


@dataclass
class RoutingDecision:
    """Outcome of RCA model routing for one incident."""

    route: str                # repeat | simple | hard
    tier: str                 # model tier actually used: cheap | main
    model: str                # model actually used for the RCA
    reason: str
    mode: str
    prior_incident_id: Optional[str] = None
    prior_summary: Optional[str] = None
    prior_title: Optional[str] = None


# ---------------------------------------------------------------------------
# Config (read per call so a values change needs only a pod restart)
# ---------------------------------------------------------------------------

def get_routing_mode() -> str:
    """off | shadow | live. Unknown values warn and degrade to off."""
    raw = (os.getenv("RCA_MODEL_ROUTING_MODE") or MODE_OFF).strip().lower()
    if raw not in _VALID_MODES:
        logger.warning("%s Unknown RCA_MODEL_ROUTING_MODE=%r; treating as 'off'", _LOG_PREFIX, raw)
        return MODE_OFF
    return raw


def get_cheap_model() -> str:
    """RCA_ROUTING_CHEAP_MODEL, stripped; empty when unset."""
    return (os.getenv("RCA_ROUTING_CHEAP_MODEL") or "").strip()


def _severity_set(var: str, default: str) -> FrozenSet[str]:
    """Parse a comma-separated, case-insensitive severity list; empty string = none."""
    raw = os.getenv(var)
    if raw is None:
        raw = default
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def get_simple_severities() -> FrozenSet[str]:
    """Severities routed as simple (RCA_ROUTING_SIMPLE_SEVERITIES)."""
    return _severity_set("RCA_ROUTING_SIMPLE_SEVERITIES", _DEFAULT_SIMPLE_SEVERITIES)


def get_main_severities() -> FrozenSet[str]:
    """Severities that always use the main model (RCA_ROUTING_MAIN_SEVERITIES)."""
    return _severity_set("RCA_ROUTING_MAIN_SEVERITIES", _DEFAULT_MAIN_SEVERITIES)


def _float_env(var: str, default: float, lo: float, hi: float) -> float:
    """Float env var within [lo, hi]; invalid, NaN or out-of-range values use default."""
    raw = os.getenv(var)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s Invalid %s=%r; using %s", _LOG_PREFIX, var, raw, default)
        return default
    if math.isnan(value) or value < lo or value > hi:
        logger.warning("%s Out-of-range %s=%r; using %s", _LOG_PREFIX, var, raw, default)
        return default
    return value


def get_repeat_lookback_hours() -> float:
    """RCA_ROUTING_REPEAT_LOOKBACK_HOURS (0 disables the title/service match)."""
    return _float_env("RCA_ROUTING_REPEAT_LOOKBACK_HOURS", _DEFAULT_LOOKBACK_HOURS, 0.0, _MAX_LOOKBACK_HOURS)


def get_repeat_min_score() -> float:
    """RCA_ROUTING_REPEAT_MIN_SCORE: minimum correlator score for a hinted repeat."""
    return _float_env("RCA_ROUTING_REPEAT_MIN_SCORE", _DEFAULT_MIN_SCORE, 0.0, 1.0)


def _orchestrator_enabled() -> bool:
    """True when the multi-agent orchestrator (own model choice) handles RCA."""
    return os.getenv("ORCHESTRATOR_ENABLED", "false").strip().lower() == "true"


def should_route(source: Optional[str], incident_id: Optional[str]) -> bool:
    """Only alert-triggered RCAs on a real incident are routed."""
    if not incident_id:
        return False
    return (source or "").strip().lower() not in _NON_ALERT_SOURCES


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------

def classify(incident: Dict[str, Any], prior: Optional[Dict[str, Any]]) -> tuple:
    """Return (route, reason) for an incident and its best prior match.

    ``prior`` is the repeat evidence found by :func:`_find_prior_rca`, or None.
    """
    severity = (incident.get("severity") or "").strip().lower()

    if severity and severity in get_main_severities():
        return ROUTE_HARD, f"severity '{severity}' always uses the main model"

    if prior:
        return ROUTE_REPEAT, (
            f"repeat of incident {prior['id']} ({prior['evidence']}), "
            "whose RCA completed on the main model"
        )

    if severity and severity in get_simple_severities():
        return ROUTE_SIMPLE, f"severity '{severity}' is in the simple list"

    if not severity:
        return ROUTE_HARD, "severity unknown; using the main model"
    return ROUTE_HARD, f"severity '{severity}' with no completed prior RCA"


def _resolve_tier(route: str, mode: str, main_model: str) -> tuple:
    """Pick (tier, model, note) for a route under the current mode."""
    if route == ROUTE_HARD:
        return TIER_MAIN, main_model, ""
    if mode != MODE_LIVE:
        return TIER_MAIN, main_model, "shadow mode: main model used"
    cheap = get_cheap_model()
    if not cheap:
        return TIER_MAIN, main_model, "RCA_ROUTING_CHEAP_MODEL unset: main model used"
    if _orchestrator_enabled():
        return TIER_MAIN, main_model, "orchestrator path picks its own models: main model used"
    return TIER_CHEAP, cheap, ""


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------

def _load_incident(cursor, incident_id: str) -> Optional[Dict[str, Any]]:
    """Fields the classifier needs for one incident, or None if not visible."""
    cursor.execute(
        """SELECT id, source_type, severity, alert_title, alert_service,
                  alert_metadata, started_at
             FROM incidents WHERE id = %s""",
        (incident_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    meta = row[5]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return {
        "id": str(row[0]),
        "source_type": row[1],
        "severity": row[2],
        "alert_title": row[3],
        "alert_service": row[4],
        "alert_metadata": meta if isinstance(meta, dict) else {},
        "started_at": row[6],
    }


# A prior RCA counts only when it completed and ran on the main model; a cheap
# RCA never seeds another cheap one, so quality can't drift down a chain.
_PRIOR_QUALIFIES = """
    i.id <> %s
    AND i.aurora_status = 'complete'
    AND i.merged_into_incident_id IS NULL
    AND i.rca_model_tier IS DISTINCT FROM 'cheap'
"""


def _hint_score(hint: Any) -> float:
    """Correlator score from a correlation_hint dict; 0.0 when absent or invalid."""
    if not isinstance(hint, dict):
        return 0.0
    try:
        return float(hint.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _prior_from_hint(cursor, incident: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Prior RCA named by the rule correlator's hint, if its score clears the bar."""
    hint = (incident.get("alert_metadata") or {}).get("correlation_hint")
    hinted_id = hint.get("incident_id") if isinstance(hint, dict) else None
    score = _hint_score(hint)
    if not hinted_id or score < get_repeat_min_score():
        return None
    # Follow a folded hint target to its group anchor.
    cursor.execute(
        f"""SELECT i.id, i.alert_title, i.aurora_summary
              FROM incidents i
             WHERE i.id = (SELECT COALESCE(recurrence_of_incident_id, id)
                             FROM incidents WHERE id = %s)
               AND {_PRIOR_QUALIFIES}""",
        (hinted_id, incident["id"]),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {"id": str(row[0]), "title": row[1], "summary": row[2],
            "evidence": f"correlator score {score:.2f}"}


def _prior_from_fingerprint(cursor, incident: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Most recent prior RCA with the same source, alert title and service."""
    title = (incident.get("alert_title") or "").strip()
    lookback = get_repeat_lookback_hours()
    if not title or lookback <= 0:
        return None
    anchor = incident.get("started_at") or datetime.now(timezone.utc).replace(tzinfo=None)
    cursor.execute(
        f"""SELECT i.id, i.alert_title, i.aurora_summary
              FROM incidents i
             WHERE {_PRIOR_QUALIFIES}
               AND i.source_type = %s
               AND i.alert_title = %s
               AND i.alert_service IS NOT DISTINCT FROM %s
               AND i.started_at >= %s - make_interval(secs => %s)
               AND i.started_at <= %s
             ORDER BY i.started_at DESC
             LIMIT 1""",
        (incident["id"], incident.get("source_type"), title,
         incident.get("alert_service"), anchor, lookback * 3600.0, anchor),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {"id": str(row[0]), "title": row[1], "summary": row[2],
            "evidence": "same alert title and service"}


def _find_prior_rca(cursor, incident: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find a completed main-model RCA this incident repeats, or None."""
    return _prior_from_hint(cursor, incident) or _prior_from_fingerprint(cursor, incident)


def _persist(cursor, incident_id: str, decision: RoutingDecision) -> None:
    """Record the routing decision on the incident row."""
    cursor.execute(
        """UPDATE incidents
              SET rca_route = %s,
                  rca_model_tier = %s,
                  rca_model = %s,
                  rca_route_reason = %s,
                  rca_routed_at = %s
            WHERE id = %s""",
        (
            decision.route,
            decision.tier,
            decision.model[:255],
            decision.reason[:_REASON_MAX_LEN],
            datetime.now(timezone.utc).replace(tzinfo=None),
            incident_id,
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def route_rca(user_id: str, incident_id: str, main_model: str) -> Optional[RoutingDecision]:
    """Run RCA model routing for one incident.

    Returns None when routing is off. Otherwise returns the decision (already
    persisted on the incident). Never raises: on any failure the main model is
    returned so RCA quality is protected.
    """
    mode = get_routing_mode()
    if mode == MODE_OFF:
        return None

    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Celery task: no Flask request, RLS must be set explicitly.
                if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX):
                    raise RuntimeError("no org for user")
                incident = _load_incident(cursor, incident_id)
                if not incident:
                    logger.warning("%s Incident %s not found; using main model", _LOG_PREFIX, incident_id)
                    return RoutingDecision(ROUTE_HARD, TIER_MAIN, main_model,
                                           "incident not found; using the main model", mode)

                prior = _find_prior_rca(cursor, incident)
                route, reason = classify(incident, prior)
                tier, model, note = _resolve_tier(route, mode, main_model)
                if note:
                    reason = f"{reason} ({note})"
                repeat_of = prior if route == ROUTE_REPEAT else {}
                decision = RoutingDecision(
                    route=route, tier=tier, model=model, reason=reason, mode=mode,
                    prior_incident_id=repeat_of.get("id"),
                    prior_title=repeat_of.get("title"),
                    prior_summary=repeat_of.get("summary") or None,
                )
                _persist(cursor, incident_id, decision)
            conn.commit()
    except Exception as e:
        logger.warning("%s Check failed for incident %s; using main model: %s",
                       _LOG_PREFIX, incident_id, type(e).__name__)
        return RoutingDecision(ROUTE_HARD, TIER_MAIN, main_model,
                               "model routing check failed; using the main model", mode)

    logger.info(
        "%s incident=%s mode=%s route=%s tier=%s model=%s reason=%s",
        _LOG_PREFIX, incident_id, mode, decision.route, decision.tier,
        decision.model, decision.reason,
    )
    return decision


def prior_rca_context(decision: Optional[RoutingDecision]) -> Optional[Dict[str, str]]:
    """Prior-RCA hint for the prompt, only when a repeat was routed cheap."""
    if not decision or decision.tier != TIER_CHEAP or decision.route != ROUTE_REPEAT:
        return None
    if not decision.prior_incident_id:
        return None
    summary = (decision.prior_summary or "").strip()
    if len(summary) > _PRIOR_SUMMARY_MAX_CHARS:
        summary = summary[:_PRIOR_SUMMARY_MAX_CHARS].rstrip() + "…"
    return {
        "incident_id": decision.prior_incident_id,
        "title": (decision.prior_title or "").strip(),
        "summary": summary,
    }

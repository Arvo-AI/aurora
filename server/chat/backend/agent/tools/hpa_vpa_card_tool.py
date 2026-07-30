"""Slack card for HPA/VPA right-sizing recommendations.

Deliberately not in ``slack_tool.py``: that module is the read-only Slack
surface. This is the **first agent-callable Slack write tool** in the repo, so
there is no prior art to copy -- the shape here (claim row -> post -> attach ts,
with a compensating delete) is new.

Card construction lives in one builder taking a neutral recommendation dict.
Slack and Google Chat button schemas are irreconcilable (``action_id`` string
prefixes vs ``function`` + typed ``parameters``), so keeping the intermediate
representation separate from the renderer makes a Google Chat card an addition
rather than a rewrite of this tool.

There is intentionally no ``tool_registry.py`` entry and no ``gate_action``
call. See the module docstring notes on each in ``send_hpa_vpa_recommendation``.
"""

import json
import logging
import math
import re
from typing import Optional

from pydantic import BaseModel, Field

from routes.slack.slack_events_helpers import SLACK_MAX_SECTION_TEXT, validate_slack_blocks
from services.actions import hpa_vpa_recommendations as recs
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# The card must not become the leak path for the prompt's numeric guardrail:
# a raw nanocore/byte integer pasted into Slack is unreadable and tends to end
# up copied into a PR body.
_LONG_INT_RE = re.compile(r"\d{10,}")
_MAX_DISPLAY_CHARS = 200
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# Column widths from the hpa_vpa_recommendations DDL. Enforced here because an
# over-long value is not a display problem: the INSERT raises "value too long",
# the whole tool call fails with a generic error, and no card is ever posted.
_COLUMN_LIMITS = {
    "workload": 255,
    "service": 255,
    "environment": 128,
    "autoscaler": 64,
    "metrics_source": 32,
    "vcs_provider": 32,
}

_DIMENSIONS = (
    ("memory", "Memory request"),
    ("cpu", "CPU request"),
    ("max_replicas", "HPA maxReplicas"),
)


class SendHpaVpaRecommendationArgs(BaseModel):
    workload: str = Field(description="Workload / deployment name, e.g. 'apiv2worker'")
    repo: str = Field(description="Repository holding the IaC change, as 'owner/repo'")
    pr_number: int = Field(description="Number of the already-opened right-sizing PR")
    pr_url: str = Field(description="Web URL of the PR, used for the View PR button")
    service: Optional[str] = Field(default=None, description="Service name if different from the workload")
    environment: Optional[str] = Field(
        default=None,
        description="Environment the workload runs in, e.g. 'production'. Part of the "
        "dedup key: the same workload in two environments is tracked separately.",
    )
    autoscaler: Optional[str] = Field(
        default=None,
        description="Autoscaler in play, e.g. 'HPA (CPU)', 'KEDA (queue depth)', 'none'. "
        "Shown on the card so a reviewer understands a partial recommendation.",
    )
    memory_current: Optional[str] = Field(default=None, description="Current memory request as display text, e.g. '2 Gi'")
    memory_recommended: Optional[str] = Field(default=None, description="Recommended memory request, e.g. '768 Mi'")
    memory_evidence: Optional[str] = Field(
        default=None, description="Short evidence, e.g. '30-day p95 usage 512 Mi -- 25% of request'"
    )
    cpu_current: Optional[str] = Field(default=None, description="Current CPU request as display text, e.g. '2000 m'")
    cpu_recommended: Optional[str] = Field(default=None, description="Recommended CPU request, e.g. '750 m'")
    cpu_evidence: Optional[str] = Field(default=None, description="Short evidence, e.g. 'p95 480 m, no throttling'")
    max_replicas_current: Optional[str] = Field(default=None, description="Current HPA maxReplicas, e.g. '10'")
    max_replicas_recommended: Optional[str] = Field(default=None, description="Recommended maxReplicas, e.g. '6'")
    max_replicas_evidence: Optional[str] = Field(default=None, description="Short evidence, e.g. 'never exceeded 4 in 30 d'")
    reviewer: Optional[str] = Field(
        default=None,
        description="Slack user ID of the reviewer (must be a real ID like 'U0966GURFUK'). "
        "Omit if unknown -- a fabricated ID renders as a blank grey box in Slack.",
    )
    severity_score: Optional[float] = Field(
        default=None,
        description="Max relative mis-size across dimensions: abs(recommended - current) / current. "
        "Used to decide whether a re-proposal during a cooldown is materially worse.",
    )
    metrics_source: Optional[str] = Field(
        default=None, description="Provider the usage percentiles came from, e.g. 'datadog'"
    )
    vcs_provider: Optional[str] = Field(
        default=None,
        description="VCS hosting the PR. Only 'github' is supported today (the default); "
        "the column exists so GitLab and Bitbucket become a fill-in rather than a migration.",
    )


class ListHpaVpaRecommendationsArgs(BaseModel):
    """No arguments -- returns every live and cooling recommendation for the org."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_display(value: Optional[str], label: str) -> Optional[str]:
    """Return an error string if a display value is unusable, else None."""
    if value is None:
        return None
    if len(value) > _MAX_DISPLAY_CHARS:
        return f"{label} is too long ({len(value)} chars, max {_MAX_DISPLAY_CHARS})"
    if _LONG_INT_RE.search(value):
        return (f"{label} contains a raw 10+ digit number. Convert to human units "
                f"(e.g. '768 Mi', '750 m') before sending the card.")
    return None


def _validate(args: dict) -> Optional[str]:
    """Validate card arguments. Returns an error message, or None when valid."""
    workload = (args.get("workload") or "").strip()
    if not workload:
        return "workload is required"

    repo = (args.get("repo") or "").strip()
    if not _REPO_RE.match(repo):
        return f"repo must be in 'owner/repo' form, got '{repo}'"

    try:
        pr_number = int(args.get("pr_number") or 0)
    except (TypeError, ValueError):
        return "pr_number must be an integer"
    if pr_number <= 0:
        return "pr_number must be a positive integer"

    pr_url = (args.get("pr_url") or "").strip()
    if not pr_url:
        return "pr_url is required so the View PR button has a destination"
    # Slack rejects a button whose url is not http(s), which fails the whole
    # chat.postMessage call -- and a javascript:/data: url would be a live XSS
    # vector in any other renderer this intermediate dict later feeds. HTTPS
    # only: every supported VCS serves PRs over TLS, so plain http here is
    # either a typo or a downgrade.
    if not pr_url.lower().startswith("https://"):
        return f"pr_url must be an https:// URL, got '{pr_url[:60]}'"

    # Reject a provider we cannot close a PR on, before a row is claimed and a
    # card is posted. Otherwise Dismiss would fail on every click, leaving the
    # PR open with no way to act on the card.
    provider = (args.get("vcs_provider") or "github").lower().strip()
    supported = recs.supported_vcs_providers()
    if provider not in supported:
        return (f"vcs_provider '{provider}' is not supported yet. "
                f"Supported: {', '.join(sorted(supported))}.")

    present = [key for key, _ in _DIMENSIONS
               if args.get(f"{key}_current") and args.get(f"{key}_recommended")]
    if not present:
        return ("No recommendation to report: at least one of memory, cpu or max_replicas "
                "needs both a current and a recommended value.")

    for key, label in _DIMENSIONS:
        for suffix in ("current", "recommended", "evidence"):
            err = _check_display(args.get(f"{key}_{suffix}"), f"{label} {suffix}")
            if err:
                return err
    for field in ("workload", "service", "environment", "autoscaler"):
        err = _check_display(args.get(field), field)
        if err:
            return err

    for field, width in _COLUMN_LIMITS.items():
        value = args.get(field)
        if isinstance(value, str) and len(value.strip()) > width:
            return f"{field} is too long ({len(value.strip())} chars, max {width})"
    return None


# ---------------------------------------------------------------------------
# Card construction (neutral dict in, Slack blocks out)
# ---------------------------------------------------------------------------


def _mention(slack_user_id: Optional[str]) -> Optional[str]:
    """Render a real Slack mention, or None.

    Only ``<@U...>`` renders as a mention; a fabricated id renders as a blank
    grey box, so an unresolvable reviewer falls back to plain text instead.
    """
    value = (slack_user_id or "").strip()
    if re.fullmatch(r"[UW][A-Z0-9]{6,}", value):
        return f"<@{value}>"
    return None


def _build_body(rec: dict, status_line: str) -> str:
    """Body section text: one line per recommended dimension, present data only."""
    lines = []
    if rec.get("service"):
        lines.append(f"*Service:* {rec['service']}")
    if rec.get("environment"):
        lines.append(f"*Environment:* {rec['environment']}")

    for key, label in _DIMENSIONS:
        current = rec.get(f"{key}_current")
        recommended = rec.get(f"{key}_recommended")
        if not (current and recommended):
            continue
        line = f"*{label}:* {current} -> *{recommended}*"
        evidence = rec.get(f"{key}_evidence")
        if evidence:
            line += f" ({evidence})"
        lines.append(line)

    if rec.get("autoscaler"):
        lines.append(f"*Autoscaler:* {rec['autoscaler']}")
    if status_line:
        lines.append(status_line)

    body = "\n".join(lines)
    if len(body) > SLACK_MAX_SECTION_TEXT:
        body = body[: SLACK_MAX_SECTION_TEXT - 3] + "..."
    return body


def build_recommendation_blocks(rec: dict, rec_id: str, *, with_actions: bool = True,
                                status_line: Optional[str] = None) -> list:
    """Build the card from a neutral recommendation dict.

    ``with_actions=False`` drops the buttons entirely, which is how the
    post-dismiss rewrite guarantees a card cannot be re-clicked.
    """
    pr_number = rec.get("pr_number")
    if status_line is None:
        reviewer = _mention(rec.get("reviewer"))
        status_line = f"*Status:* PR #{pr_number} open -- awaiting review"
        if reviewer:
            status_line += f" by {reviewer}"

    blocks = [
        # Slack groups consecutive messages from the same app, which visually
        # fuses cards into whatever preceded them. A leading divider makes each
        # card read as its own unit (build_suggestions_blocks does the same).
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": "Right-Sizing Recommendation"}},
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"_Recommendation for_ `{rec.get('workload', 'unknown')}`"}},
    ]

    if with_actions:
        # An actions block, not a section accessory: Slack's section `accessory`
        # takes a single element object, not an array, so the mockup's two
        # flush-right buttons are not expressible. This keeps the pair together.
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": f"View PR #{pr_number}"},
                 "url": rec.get("pr_url"), "style": "primary",
                 "action_id": f"hpa_vpa_view_pr_{rec_id}"},
                {"type": "button", "text": {"type": "plain_text", "text": "Dismiss"},
                 "value": rec_id, "action_id": f"hpa_vpa_dismiss_{rec_id}"},
            ],
        })

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _build_body(rec, status_line)}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": "Nothing is applied until the PR is approved and merged by your team."}
    ]})
    return blocks


def _fallback_text(rec: dict) -> str:
    return f"Right-Sizing Recommendation: {rec.get('workload', 'unknown')} (PR #{rec.get('pr_number')})"


def _recommendation_payload(args: dict) -> dict:
    """Per-dimension {current, recommended, evidence} for the JSONB column."""
    payload = {}
    for key, _label in _DIMENSIONS:
        current = args.get(f"{key}_current")
        recommended = args.get(f"{key}_recommended")
        if current and recommended:
            payload[key] = {
                "current": current,
                "recommended": recommended,
                "evidence": args.get(f"{key}_evidence"),
            }
    return payload


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def send_hpa_vpa_recommendation(user_id: Optional[str] = None, **kwargs) -> str:
    """Post (or update) the Slack card for one right-sized workload.

    Ordering matters. The cooldown is checked and the row claimed *before* the
    card goes out, and the caller is expected to have checked
    ``list_hpa_vpa_recommendations`` before opening the PR -- otherwise a
    suppressed workload still gets a PR nobody asked for.

    Returns a JSON string. Never raises: a failure here must not abort the
    agent loop mid-run.
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    error = _validate(kwargs)
    if error:
        return json.dumps({"error": error})

    workload = kwargs["workload"].strip()
    environment = (kwargs.get("environment") or "").strip() or None
    workload_key = recs.build_workload_key(workload, environment)
    repo_full_name = kwargs["repo"].strip()
    pr_number = int(kwargs["pr_number"])
    vcs_provider = (kwargs.get("vcs_provider") or "github").lower().strip()
    payload = _recommendation_payload(kwargs)
    severity = kwargs.get("severity_score")
    if severity is not None:
        try:
            severity = float(severity)
        except (TypeError, ValueError):
            severity = None
    # A NaN/inf severity would clear is_materially_worse's isinstance check and
    # then compare as >= anything (inf) or as never-worse (NaN), i.e. the LLM
    # could break any cooldown by passing Infinity. Treat it as unknown.
    if severity is not None and not math.isfinite(severity):
        logger.warning("[HpaVpaCard] Discarding non-finite severity_score for %s", sanitize(workload_key))
        severity = None

    rec = {
        "workload": workload,
        "service": (kwargs.get("service") or "").strip() or None,
        "environment": environment,
        "autoscaler": (kwargs.get("autoscaler") or "").strip() or None,
        "pr_number": pr_number,
        "pr_url": kwargs["pr_url"].strip(),
        "reviewer": kwargs.get("reviewer"),
        **{f"{k}_{s}": kwargs.get(f"{k}_{s}")
           for k, _ in _DIMENSIONS for s in ("current", "recommended", "evidence")},
    }

    # Resolve Slack up-front, OUTSIDE the DB block. Both helpers take their own
    # pool connection internally, so calling them while this function holds one
    # would nest two checkouts per invocation and can deadlock the pool under
    # concurrency. Doing it first also fails fast before a row is claimed.
    slack = _resolve_slack_target(user_id)
    if slack.get("error"):
        return json.dumps({"error": slack["error"]})

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, user_id, log_prefix="[HpaVpaCard]")
                if not org_id:
                    return json.dumps({"error": "Could not resolve organization context"})

                recs.lock_workload(cur, org_id, workload_key)

                cooldown = recs.get_active_cooldown(cur, org_id, workload_key)
                if cooldown:
                    if not recs.is_materially_worse(severity, cooldown.get("severity_score")):
                        conn.commit()
                        logger.info(
                            "[HpaVpaCard] Suppressed %s for org=%s (cooldown until %s)",
                            sanitize(workload_key), sanitize(org_id), cooldown.get("cooldown_until"),
                        )
                        return json.dumps({
                            "status": "suppressed",
                            "reason": "cooldown",
                            "workload": workload,
                            "cooldown_until": cooldown.get("cooldown_until"),
                            "dismissed_severity_score": cooldown.get("severity_score"),
                            "message": (
                                "A human dismissed this workload and the cooldown is still "
                                "running, and the mis-size has not materially worsened. No card "
                                "was posted. This is the anti-nag rule working -- record it in "
                                "the living document and move on. Do not work around it."
                            ),
                        })
                    # Materially worse: retire the dismissal and fall through.
                    recs.mark_superseded(cur, cooldown["id"])
                    logger.info(
                        "[HpaVpaCard] Cooldown superseded for %s (severity %s -> %s)",
                        sanitize(workload_key), cooldown.get("severity_score"), severity,
                    )

                live = recs.get_live_recommendation(cur, org_id, workload_key)
                if live:
                    return _update_existing(conn, cur, slack, live, rec, payload, severity,
                                            repo_full_name, pr_number, kwargs)

                rec_id = recs.claim_recommendation(
                    cur, org_id, user_id,
                    workload_key=workload_key, workload=workload, environment=environment,
                    service=rec["service"], autoscaler=rec["autoscaler"],
                    metrics_source=(kwargs.get("metrics_source") or None),
                    vcs_provider=vcs_provider, repo_full_name=repo_full_name,
                    pr_number=pr_number, pr_url=rec["pr_url"],
                    recommendation=payload, severity_score=severity,
                )
                conn.commit()

                return _post_new(conn, cur, slack, rec_id, rec)
    except Exception:
        logger.exception("[HpaVpaCard] Failed to send recommendation for user=%s", sanitize(user_id))
        return json.dumps({"error": "Could not post the right-sizing recommendation card"})


def _resolve_slack_target(user_id: str) -> dict:
    """Resolve the Slack client and destination channel for a user.

    Called before any DB connection is checked out: both helpers below take
    their own pool connection, so resolving them inside an open transaction
    would hold two checkouts at once.
    """
    from connectors.slack_connector.client import get_slack_client_for_user
    from utils.notifications.slack_notification_service import _get_incidents_channel_id

    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return {"error": "Slack is not connected for this user"}
        channel_id = _get_incidents_channel_id(user_id, client)
        if not channel_id:
            return {"error": "No Slack incidents channel is configured"}
        return {"client": client, "channel_id": channel_id}
    except Exception as exc:
        logger.exception("[HpaVpaCard] Could not resolve Slack target for user=%s", sanitize(user_id))
        return {"error": f"Could not resolve Slack destination: {type(exc).__name__}"}


def _post_new(conn, cur, slack: dict, rec_id: str, rec: dict) -> str:
    """Post a fresh card and attach its ts, releasing the claim on failure."""
    client, channel_id = slack["client"], slack["channel_id"]

    try:
        blocks = build_recommendation_blocks(rec, rec_id)
        text = _fallback_text(rec)
        if not validate_slack_blocks(blocks):
            logger.error("[HpaVpaCard] Block validation failed; posting plain text for %s", sanitize(rec["workload"]))
            blocks = None

        response = client.send_message(channel=channel_id, text=text, blocks=blocks)
        message_ts = (response or {}).get("ts")
        if not message_ts:
            raise ValueError("Slack did not return a message timestamp")
    except Exception as exc:
        # Compensate: a transient Slack outage must not permanently block this
        # workload behind the partial unique index.
        try:
            recs.delete_recommendation(cur, rec_id)
            conn.commit()
        except Exception:
            logger.exception("[HpaVpaCard] Failed to release claimed row %s", rec_id)
        logger.exception("[HpaVpaCard] Slack post failed for %s", sanitize(rec["workload"]))
        return json.dumps({"error": f"Could not post the Slack card: {type(exc).__name__}: {str(exc)[:150]}"})

    recs.attach_slack_message(cur, rec_id, channel_id, message_ts)
    conn.commit()
    logger.info("[HpaVpaCard] Posted card for %s (rec=%s)", sanitize(rec["workload"]), rec_id)
    return json.dumps({
        "status": "posted",
        "recommendation_id": rec_id,
        "workload": rec["workload"],
        "channel": channel_id,
        "pr_number": rec["pr_number"],
    })


def _update_existing(conn, cur, slack: dict, live: dict, rec: dict, payload: dict,
                     severity: Optional[float], repo_full_name: str, pr_number: int,
                     kwargs: dict) -> str:
    """Refresh an open recommendation in place -- one card per workload, ever."""
    rec_id = live["id"]
    recs.refresh_recommendation(
        cur, rec_id, recommendation=payload, severity_score=severity,
        repo_full_name=repo_full_name, pr_number=pr_number, pr_url=rec["pr_url"],
        autoscaler=rec["autoscaler"], metrics_source=(kwargs.get("metrics_source") or None),
    )
    conn.commit()

    blocks = build_recommendation_blocks(rec, rec_id)
    text = _fallback_text(rec)
    if not validate_slack_blocks(blocks):
        blocks = None

    channel_id, message_ts = live.get("slack_channel_id"), live.get("slack_message_ts")
    client = slack["client"]
    try:
        if channel_id and message_ts:
            client.update_message(channel=channel_id, ts=message_ts, text=text, blocks=blocks)
            logger.info("[HpaVpaCard] Updated card in place for %s (rec=%s)", sanitize(rec["workload"]), rec_id)
            return json.dumps({
                "status": "updated",
                "recommendation_id": rec_id,
                "workload": rec["workload"],
                "message": "Existing card updated in place with fresh numbers -- no duplicate posted.",
            })
    except Exception:
        # Message deleted, or a stale ts. Fall through and post a replacement.
        logger.warning("[HpaVpaCard] update_message failed for rec=%s; reposting", rec_id)

    return _post_new_for_existing(conn, cur, slack, rec_id, rec)


def _post_new_for_existing(conn, cur, slack: dict, rec_id: str, rec: dict) -> str:
    """Repost a card for a row that already exists, overwriting its ts.

    Distinct from :func:`_post_new` only in that a failure must NOT delete the
    row -- the recommendation predates this post attempt.
    """
    client, channel_id = slack["client"], slack["channel_id"]

    try:
        blocks = build_recommendation_blocks(rec, rec_id)
        if not validate_slack_blocks(blocks):
            blocks = None
        response = client.send_message(channel=channel_id, text=_fallback_text(rec), blocks=blocks)
        message_ts = (response or {}).get("ts")
        if not message_ts:
            raise ValueError("Slack did not return a message timestamp")
    except Exception as exc:
        logger.exception("[HpaVpaCard] Repost failed for rec=%s", rec_id)
        return json.dumps({"error": f"Could not post the Slack card: {type(exc).__name__}: {str(exc)[:150]}"})

    recs.attach_slack_message(cur, rec_id, channel_id, message_ts)
    conn.commit()
    return json.dumps({
        "status": "reposted",
        "recommendation_id": rec_id,
        "workload": rec["workload"],
        "message": "The previous card was gone, so a replacement was posted.",
    })


def list_hpa_vpa_recommendations(user_id: Optional[str] = None, **kwargs) -> str:
    """List open right-sizing recommendations and workloads still in cooldown.

    Call this BEFORE opening any PR. Checking after the fact produces the worst
    sequence: open PR -> post card -> suppressed, leaving a PR nobody wanted.
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})
    return json.dumps(recs.list_recommendations(user_id))


HPA_VPA_TOOL_SPECS = [
    (
        send_hpa_vpa_recommendation,
        "send_hpa_vpa_recommendation",
        SendHpaVpaRecommendationArgs,
        "Post a Slack card to the incidents channel for ONE workload you have already opened a "
        "right-sizing PR for. The card carries View PR and Dismiss buttons. Pass current and "
        "recommended values as display strings in human units ('2 Gi' -> '768 Mi', '2000 m' -> "
        "'750 m') -- never raw byte or nanocore integers. Omit any dimension you are not "
        "recommending a change to; the card degrades cleanly. Returns status 'posted', 'updated', "
        "or 'suppressed' (a human dismissed this workload and the cooldown is still running -- "
        "respect it, do not work around it).",
    ),
    (
        list_hpa_vpa_recommendations,
        "list_hpa_vpa_recommendations",
        ListHpaVpaRecommendationsArgs,
        "List right-sizing recommendations already open, plus workloads a human dismissed that "
        "are still inside their cooldown window. Call this BEFORE opening any right-sizing PR so "
        "you update an existing proposal instead of opening a duplicate, and skip workloads that "
        "are suppressed.",
    ),
]

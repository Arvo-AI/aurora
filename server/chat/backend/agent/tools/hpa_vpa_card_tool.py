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


def _validate_pr_reference(args: dict) -> Optional[str]:
    """Validate the repo / PR number / PR URL / provider quartet."""
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
    return None


def _validate_display_values(args: dict) -> Optional[str]:
    """Validate every human-facing string against length, digits and DDL width."""
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


def _validate(args: dict) -> Optional[str]:
    """Validate card arguments. Returns an error message, or None when valid."""
    if not (args.get("workload") or "").strip():
        return "workload is required"

    err = _validate_pr_reference(args)
    if err:
        return err

    present = [key for key, _ in _DIMENSIONS
               if args.get(f"{key}_current") and args.get(f"{key}_recommended")]
    if not present:
        return ("No recommendation to report: at least one of memory, cpu or max_replicas "
                "needs both a current and a recommended value.")

    return _validate_display_values(args)


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
                                status_line: Optional[str] = None,
                                link_only: bool = False) -> list:
    """Build the card from a neutral recommendation dict.

    ``with_actions=False`` drops the buttons entirely, which is how the
    post-dismiss rewrite guarantees a card cannot be re-clicked.

    ``link_only=True`` keeps just the View PR button. Used when a dismissal could
    not close the PR: that card asks a human to go close it by hand, so deleting
    the only link to it would be actively unhelpful. View PR is a URL link-out
    with no ``value``, so it carries no re-clickable action.
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

    if with_actions or link_only:
        # An actions block, not a section accessory: Slack's section `accessory`
        # takes a single element object, not an array, so the mockup's two
        # flush-right buttons are not expressible. This keeps the pair together.
        elements = []
        # Slack rejects a url-less link button and fails the whole call, so the
        # View PR button is dropped when there is no destination -- but only that
        # button. Dropping the whole row would take Dismiss with it and leave a
        # 'proposed' row nobody can retire, blocking the workload forever behind
        # the partial unique index.
        if rec.get("pr_url"):
            elements.append(
                {"type": "button", "text": {"type": "plain_text", "text": f"View PR #{pr_number}"},
                 "url": rec.get("pr_url"), "style": "primary",
                 "action_id": f"hpa_vpa_view_pr_{rec_id}"}
            )
        if with_actions:
            elements.append(
                {"type": "button", "text": {"type": "plain_text", "text": "Dismiss"},
                 "value": rec_id, "action_id": f"hpa_vpa_dismiss_{rec_id}"}
            )
        if elements:
            blocks.append({"type": "actions", "elements": elements})

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


def _normalize_args(kwargs: dict) -> dict:
    """Turn validated tool arguments into the normalized values the flow needs.

    Returns the neutral ``rec`` dict the card renderer consumes, plus the derived
    dedup key, JSONB payload and severity. Kept separate so the tool body reads as
    the ordering it has to guarantee (lock -> cooldown -> claim -> post) rather
    than as argument marshalling.
    """
    workload = kwargs["workload"].strip()
    environment = (kwargs.get("environment") or "").strip() or None
    payload = _recommendation_payload(kwargs)
    workload_key = recs.build_workload_key(workload, environment)
    pr_number = int(kwargs["pr_number"])

    # Severity is DERIVED from the current/recommended values, never taken from
    # the model. It decides whether a human's 30-day cooldown can be broken
    # early, and the model is also the party reporting how bad the mis-size is --
    # so letting it supply the number lets it grade its own exam. Parsing the
    # display strings it already has to state keeps the card and the gate
    # consistent by construction. Unparseable values yield None, which never
    # breaks a cooldown.
    severity = recs.severity_from_display(payload)
    if severity is None:
        logger.info(
            "[HpaVpaCard] No severity derivable from display values for %s; "
            "cooldowns cannot be broken for this recommendation",
            sanitize(workload_key),
        )

    return {
        "workload_key": workload_key,
        "payload": payload,
        "severity": severity,
        "repo_full_name": kwargs["repo"].strip(),
        "pr_number": pr_number,
        "vcs_provider": (kwargs.get("vcs_provider") or "github").lower().strip(),
        "metrics_source": (kwargs.get("metrics_source") or None),
        "rec": {
            "workload": workload,
            "service": (kwargs.get("service") or "").strip() or None,
            "environment": environment,
            "autoscaler": (kwargs.get("autoscaler") or "").strip() or None,
            "pr_number": pr_number,
            "pr_url": kwargs["pr_url"].strip(),
            "reviewer": kwargs.get("reviewer"),
            **{f"{k}_{s}": kwargs.get(f"{k}_{s}")
               for k, _ in _DIMENSIONS for s in ("current", "recommended", "evidence")},
        },
    }


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

    card = _normalize_args(kwargs)
    workload_key = card["workload_key"]

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

                suppressed, superseded_id = _check_cooldown(
                    conn, cur, org_id, workload_key, card["rec"]["workload"], card["severity"]
                )
                if suppressed:
                    return suppressed

                live = recs.get_live_recommendation(cur, org_id, workload_key)
                if live:
                    return _update_existing(conn, cur, slack, live, card,
                                            superseded_id=superseded_id)

                rec_id = recs.claim_recommendation(cur, org_id, user_id, recs.WorkloadRecommendation(
                    workload_key=workload_key, workload=card["rec"]["workload"],
                    environment=card["rec"]["environment"], service=card["rec"]["service"],
                    autoscaler=card["rec"]["autoscaler"], metrics_source=card["metrics_source"],
                    vcs_provider=card["vcs_provider"], repo_full_name=card["repo_full_name"],
                    pr_number=card["pr_number"], pr_url=card["rec"]["pr_url"],
                    recommendation=card["payload"], severity_score=card["severity"],
                ))
                conn.commit()

                return _post_new(conn, cur, slack, rec_id, card["rec"],
                                 superseded_id=superseded_id)
    except Exception:
        logger.exception("[HpaVpaCard] Failed to send recommendation for user=%s", sanitize(user_id))
        return json.dumps({"error": "Could not post the right-sizing recommendation card"})


def _check_cooldown(conn, cur, org_id: str, workload_key: str, workload: str,
                    severity: Optional[float]) -> tuple:
    """Apply the anti-nag gate before anything is claimed or posted.

    Returns ``(suppression_response, superseded_rec_id)``. A non-None first
    element is the tool's final answer and the caller must return it as-is.

    The second element is the compensation handle: when a cooldown is broken, the
    dismissal is retired *before* the card that justifies it exists, so a failed
    post has to be able to put it back. Without that, the human's remaining
    anti-nag window is lost to a card nobody ever saw.
    """
    cooldown = recs.get_active_cooldown(cur, org_id, workload_key)
    if not cooldown:
        return None, None

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
        }), None

    # Materially worse: retire the dismissal and fall through to post.
    recs.mark_superseded(cur, cooldown["id"])
    logger.info(
        "[HpaVpaCard] Cooldown superseded for %s (severity %s -> %s)",
        sanitize(workload_key), cooldown.get("severity_score"), severity,
    )
    return None, cooldown["id"]


def _restore_cooldown(conn, cur, superseded_id: Optional[str], workload: str) -> None:
    """Put back a dismissal that was retired for a card that never posted."""
    if not superseded_id:
        return
    try:
        # A prior compensating statement in this transaction may have failed and
        # left it aborted, in which case every further statement raises
        # InFailedSqlTransaction. Roll back first so this UPDATE runs in a clean
        # transaction -- the earlier work is already committed or already lost.
        conn.rollback()
        if recs.restore_superseded(cur, superseded_id):
            conn.commit()
            logger.info(
                "[HpaVpaCard] Restored superseded cooldown %s after a failed post for %s",
                superseded_id, sanitize(workload),
            )
    except Exception:
        # Logged loudly: the consequence is a workload a human dismissed being
        # re-proposed on the next run, which is the exact nagging this feature
        # exists to prevent.
        logger.exception(
            "[HpaVpaCard] Could not restore superseded cooldown %s; the remaining "
            "anti-nag window for %s is lost", superseded_id, sanitize(workload),
        )


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


def _post_new(conn, cur, slack: dict, rec_id: str, rec: dict,
              *, superseded_id: Optional[str] = None) -> str:
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
        # And if a cooldown was broken to make room for this card, put it back --
        # the card that justified retiring it does not exist.
        _restore_cooldown(conn, cur, superseded_id, rec["workload"])
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


def _update_existing(conn, cur, slack: dict, live: dict, card: dict,
                     *, superseded_id: Optional[str] = None) -> str:
    """Refresh an open recommendation in place -- one card per workload, ever."""
    rec_id = live["id"]
    rec = card["rec"]
    recs.refresh_recommendation(
        cur, rec_id, recommendation=card["payload"], severity_score=card["severity"],
        repo_full_name=card["repo_full_name"], pr_number=card["pr_number"],
        pr_url=rec["pr_url"], vcs_provider=card["vcs_provider"],
        autoscaler=rec["autoscaler"], metrics_source=card["metrics_source"],
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

    return _post_new_for_existing(conn, cur, slack, rec_id, rec, superseded_id=superseded_id)


def _post_new_for_existing(conn, cur, slack: dict, rec_id: str, rec: dict,
                           *, superseded_id: Optional[str] = None) -> str:
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
        _restore_cooldown(conn, cur, superseded_id, rec["workload"])
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
        "'750 m') -- never raw byte or nanocore integers, and keep Kubernetes unit casing exact "
        "('750m' is millicores, '750M' is megabytes). Omit any dimension you are not "
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

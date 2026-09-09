"""Correlation agent for root-cause recurrence detection (dedup layer 1).

One agent with read tools answers "is this incident a recurrence of something
we already have?" and returns ``{recurrence_of, reasoning}`` via a terminating
tool. Biased to "new" on ambiguity, timeout, or malformed output. The entry
point ``run_recurrence_check`` is sync and never raises — every failure path
degrades to today's behavior (standalone incident, notifications still sent).
"""

import asyncio
import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from services.correlation.recurrence_config import (
    GROUP_IDLE_HOURS,
    MAX_TURNS,
    MODE_LIVE,
    MODE_OFF,
    REJECT_ERROR,
    REJECT_INVALID_ID,
    REJECT_NO_VERDICT,
    REJECT_SELF_REFERENCE,
    REJECT_TIMEOUT,
    get_agent_timeout_seconds,
    get_recurrence_mode,
)
from services.correlation.recurrence_fold import (
    RecurrenceVerdict,
    fold_incident,
    get_existing_verdict,
    persist_verdict,
)
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.query_helpers import iso_utc
from utils.validation import is_valid_uuid

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[RECURRENCE]"

# Recent incidents handed to the agent in its input block: the only ones whose
# group can still be joined (GROUP_IDLE_HOURS). Saves the agent from having to
# discover them with list_incidents, where a wrong status filter (completed
# investigations are 'analyzed', not 'resolved') returned nothing.
RECENT_CANDIDATES_LIMIT = 15

try:  # Celery soft-limit signal must reach the outer handler, not the
    # generic agent-failure catch (SoftTimeLimitExceeded subclasses Exception).
    from celery.exceptions import SoftTimeLimitExceeded as _SoftTimeLimit
except ImportError:  # pragma: no cover - celery always present in workers
    _SoftTimeLimit = None

_PROMPT_PATH = Path(__file__).with_name("recurrence_prompt.md")

# Write/action tools withheld from the recurrence agent by name. The agent
# gets the full read toolset (incident history + memory + connected
# cloud/observability reads); everything that mutates infrastructure, VCS,
# tickets, or documents — or dispatches further work — is denied. Do NOT use
# select_tools_for_role here: introspection tools lack _TOOL_METADATA tags and
# would be dropped.
EXCLUDED_TOOL_NAMES = frozenset({
    # execution / infrastructure mutation
    "terminal_exec",
    "cloud_exec",
    "iac_tool",
    "tailscale_ssh",
    "on_prem_kubectl",
    # VCS / fix writers (gitlab tool bundles apply_fix/push/delete actions)
    "github_commit",
    "github_fix",
    "bitbucket_fix",
    "gitlab",
    # Bitbucket action bundles: each bundles write actions (edit_file,
    # create_branch, merge_pr, trigger_pipeline, ...) that are only gated
    # read-only for background *RCA* sessions (_is_background_rca needs
    # rca_context.source, which this agent does not set) — deny wholesale,
    # same treatment as the gitlab bundle.
    "bitbucket_repos",
    "bitbucket_branches",
    "bitbucket_pull_requests",
    "bitbucket_issues",
    "bitbucket_pipelines",
    # infrastructure mutation (DNS updates, firewall toggles, cache purge)
    "cloudflare_action",
    # dispatchers
    "trigger_rca",
    "trigger_action",
    # CD platform tool with a trigger_pipeline action
    "spinnaker_rca",
    # persistent writers
    "save_postmortem",
    "write_artifact",
    "rag_index_zip",
    "save_discovery_finding",
    "save_infrastructure_context",
    # ticketing writes
    "jira_add_comment",
    "jira_create_issue",
    "jira_update_issue",
    "jira_link_issues",
    # doc-store writes (background mode already restricts Notion to its RCA
    # subset; notion_create_* members are covered by the prefix list below)
    "notion_export_postmortem",
    "sharepoint_create_page",
})

# Belt-and-braces prefixes for write verbs on doc stores.
_EXCLUDED_TOOL_PREFIXES = (
    "notion_create",
    "notion_update",
    "notion_delete",
    "notion_move",
    "notion_duplicate",
    "notion_trash",
    "notion_upload",
    "notion_append",
)


@lru_cache(maxsize=1)
def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _is_excluded_tool(name: str) -> bool:
    if name in EXCLUDED_TOOL_NAMES:
        return True
    if name.startswith(_EXCLUDED_TOOL_PREFIXES):
        return True
    if name.startswith("mcp_"):
        # Runtime MCP tools are registered as mcp_{server}_{tool}; the
        # destructive classifier's sets hold the *unprefixed* tool names, so
        # strip the prefix before classifying. Fail CLOSED: if the classifier
        # cannot run, deny the MCP tool rather than hand an unclassified
        # external tool to an unattended agent.
        parts = name.split("_", 2)
        original = parts[2] if len(parts) == 3 else name
        try:
            from chat.backend.agent.tools.mcp_tools import is_destructive_mcp_tool

            return is_destructive_mcp_tool(original)
        except Exception:
            logger.warning(
                "%s is_destructive_mcp_tool unavailable; excluding MCP tool %s",
                _LOG_PREFIX, name, exc_info=True,
            )
            return True
    return False


def make_submit_verdict_tool(captured: Dict[str, Any]):
    """Terminating tool: captures the agent's verdict into *captured* by closure.

    Schema validation happens in the tool-invocation layer (StructuredTool
    validates against the args schema before the function runs; langgraph's
    ToolNode surfaces a validation failure to the model as a tool error), so
    the body only normalizes null-ish strings.
    """
    from langchain_core.tools import StructuredTool

    def submit_correlation_verdict(
        reasoning: str, recurrence_of: Optional[str] = None
    ) -> str:
        claim = (recurrence_of or "").strip()
        if claim.lower() in ("", "null", "none"):
            claim = None
        captured["verdict"] = RecurrenceVerdict(
            recurrence_of=claim, reasoning=reasoning
        )
        return "Verdict recorded. Stop now — do not call any more tools."

    return StructuredTool.from_function(
        func=submit_correlation_verdict,
        name="submit_correlation_verdict",
        description=(
            "Submit your final recurrence verdict. Call exactly once, as your "
            "last action. Pass recurrence_of=null for a new incident, or the "
            "id of the incident this one is a recurrence of."
        ),
        args_schema=RecurrenceVerdict,
    )


def _build_input_block(ctx: Dict[str, Any], incident_id: str, decision_point: str) -> str:
    lines = [
        "## Incident under examination",
        "",
        f"- id: {incident_id}",
        f"- title: {ctx.get('title') or '(none)'}",
        f"- service: {ctx.get('service') or '(unknown)'}",
        f"- source: {ctx.get('source_type') or '(unknown)'}",
        f"- severity: {ctx.get('severity') or '(unknown)'}",
        f"- fired at: {ctx.get('fired_at_iso') or '(unknown)'}",
    ]
    if decision_point == "after":
        lines += [
            "",
            "### Completed investigation conclusion",
            "",
            ctx.get("summary") or "(no summary available)",
        ]
    # Primary candidate map: the org's Incident Index (compact, id-keyed history).
    # The agent scans this for anchors, then drills in via get_incident.
    lines += ["", *_incident_index_lines(ctx.get("incident_index"))]
    # Bounded fallback / joinability guard: the incidents whose group can still
    # be joined right now (last GROUP_IDLE_HOURS, capped at RECENT_CANDIDATES_LIMIT).
    lines += ["", *_recent_incidents_lines(ctx.get("recent"), truncated=bool(ctx.get("recent_truncated")))]
    hint = ctx.get("hint")
    if isinstance(hint, dict) and hint.get("incident_id"):
        lines += [
            "",
            "### Rule correlator hint (verify — not a verdict)",
            "",
            (
                f"The rule correlator scored this alert {hint.get('score')} against "
                f"incident {hint.get('incident_id')} (strategy: {hint.get('strategy')}). "
                "It only sees titles, services, and timing."
            ),
        ]
    return "\n".join(lines)


def _incident_index_lines(index_content: Optional[str]) -> list:
    """Input-block section presenting the Incident Index as the candidate map.

    This is the primary recurrence-discovery surface: a compact, id-keyed list
    of past incidents the agent scans to find an anchor, then confirms with
    get_incident. Bounded by INDEX_INJECTION_CHAR_BUDGET at read time. Empty on
    cold start — the recent-incidents section below is then the only candidate
    source (graceful fallback)."""
    header = "### Incident Index (past incidents, newest last — the recurrence candidate map)"
    if not index_content or not index_content.strip():
        return [
            header,
            "",
            "(empty) — no index yet. Use the recent-incidents list below and "
            "list_incidents/get_incident to find any prior related incident.",
        ]
    return [
        header,
        "",
        "Each line is `- [INC <id> | <date> | <service> | <status>] <synopsis>`. "
        "To confirm a candidate, call get_incident(<id>) for its full conclusion "
        "before claiming a recurrence. Same monitor or service is NOT enough — "
        "the root cause must match.",
        "",
        index_content.strip(),
    ]


def _recent_incidents_lines(recent: Optional[list], *, truncated: bool = False) -> list:
    """Input-block section listing the incidents eligible to be joined.
    `truncated` says the window holds more than RECENT_CANDIDATES_LIMIT: the
    list is then a sample, never presented as the complete set of open groups."""
    header = f"### Recent incidents in this org (last {GROUP_IDLE_HOURS}h, newest first)"
    if not recent:
        return [
            header,
            "",
            "(none) — no group is open to join; unless a tool call surfaces an "
            f"incident that fired within the last {GROUP_IDLE_HOURS}h, answer new.",
        ]
    lines = [
        header,
        "",
        f"Only incidents that fired within the last {GROUP_IDLE_HOURS}h, or the group roots of "
        f"such incidents, can be claimed. A group with no activity in {GROUP_IDLE_HOURS}h is "
        "closed and a claim on it is rejected.",
        "",
    ]
    for r in recent:
        line = (
            f"- {r['id']} | fired {r.get('fired_at_iso') or '?'} | {r.get('status') or '?'}"
            f" | {r.get('service') or '(no service)'} | {r.get('title') or '(no title)'}"
        )
        if r.get("recurrence_of"):
            line += f" | recurrence of {r['recurrence_of']}"
        lines.append(line)
    if truncated:
        lines.append(
            f"- (more incidents fired in the last {GROUP_IDLE_HOURS}h than shown here — use "
            "list_incidents without a status filter to see them; any of them, or their "
            "group roots, can also be claimed)"
        )
    return lines


def _fetch_recent_incidents(cursor, incident_id: str) -> Tuple[list, bool]:
    """Incidents in the caller's org (RLS) that fired within GROUP_IDLE_HOURS,
    newest first, excluding the one under examination, capped at
    RECENT_CANDIDATES_LIMIT — plus whether the window held more than that.
    Same window and clock as recurrence_fold._group_is_stale so what the agent
    sees is what the fold will accept."""
    cursor.execute(
        """SELECT id, alert_title, alert_service, status,
                  COALESCE(alert_fired_at, started_at) AS fired_at,
                  recurrence_of_incident_id
           FROM incidents
           WHERE id <> %s
             AND status <> 'merged'
             AND COALESCE(alert_fired_at, started_at)
                 >= (NOW() AT TIME ZONE 'UTC') - make_interval(hours => %s)
           ORDER BY COALESCE(alert_fired_at, started_at) DESC
           LIMIT %s""",
        (incident_id, GROUP_IDLE_HOURS, RECENT_CANDIDATES_LIMIT + 1),
    )
    rows = cursor.fetchall()
    truncated = len(rows) > RECENT_CANDIDATES_LIMIT
    return [
        {
            "id": str(r[0]),
            "title": r[1] or "",
            "service": r[2] or "",
            "status": r[3] or "",
            "fired_at_iso": iso_utc(r[4]),
            "recurrence_of": str(r[5]) if r[5] else None,
        }
        for r in rows[:RECENT_CANDIDATES_LIMIT]
    ], truncated


def _fetch_incident_context(incident_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                if not org_id:
                    logger.warning(
                        "%s[RLS-MISS] No org for user %s; skipping recurrence check for %s",
                        _LOG_PREFIX, user_id, incident_id,
                    )
                    return None
                cursor.execute(
                    """SELECT alert_title, alert_service, source_type, severity,
                              started_at, alert_fired_at, aurora_summary, alert_metadata
                       FROM incidents WHERE id = %s""",
                    (incident_id,),
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning(
                        "%s Incident %s not found under RLS; skipping check",
                        _LOG_PREFIX, incident_id,
                    )
                    return None
                meta = row[7] or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}
                hint = meta.get("correlation_hint") if isinstance(meta, dict) else None
                started_at, fired_at = row[4], row[5] or row[4]
                recent, recent_truncated = _fetch_recent_incidents(cursor, incident_id)
                # Incident Index = primary candidate map. Read here (same RLS
                # connection semantics via its own helper) so it's injected into
                # the input block deterministically — no embeddings, one artifact.
                from services.memory.incident_index import read_index
                incident_index = read_index(user_id)
                return {
                    "org_id": org_id,
                    "title": row[0] or "",
                    "service": row[1] or "",
                    "source_type": row[2] or "",
                    "severity": row[3] or "",
                    "started_at_iso": iso_utc(started_at),
                    "fired_at_iso": iso_utc(fired_at),
                    "summary": row[6] or "",
                    "hint": hint if isinstance(hint, dict) else None,
                    "recent": recent,
                    "recent_truncated": recent_truncated,
                    "incident_index": incident_index,
                }
    except Exception:
        logger.exception(
            "%s Failed to fetch incident context for %s", _LOG_PREFIX, incident_id
        )
        return None


def _clamp_claimed_id(
    claimed: str, incident_id: str, user_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """Server-side authority on the agent's claim: (accepted_id, reject_reason).

    Only ids that exist in this org under RLS are accepted; invented or
    malformed ids clamp to invalid_id -> "new".
    """
    if not is_valid_uuid(claimed):
        return None, REJECT_INVALID_ID
    if str(claimed) == str(incident_id):
        return None, REJECT_SELF_REFERENCE
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                if not org_id:
                    logger.warning(
                        "%s[RLS-MISS] No org for user %s; cannot validate claim %s",
                        _LOG_PREFIX, user_id, claimed,
                    )
                    return None, REJECT_ERROR
                cursor.execute("SELECT id FROM incidents WHERE id = %s", (claimed,))
                row = cursor.fetchone()
                if not row:
                    return None, REJECT_INVALID_ID
                return str(row[0]), None
    except Exception:
        logger.exception("%s Failed to validate claimed id %s", _LOG_PREFIX, claimed)
        return None, REJECT_ERROR


async def _run_agent(
    *,
    incident_id: str,
    user_id: str,
    session_id: Optional[str],
    org_id: Optional[str],
    ctx: Dict[str, Any],
    decision_point: str,
    captured: Dict[str, Any],
) -> None:
    """Sub-agent-style construction (see orchestrator/sub_agent.py): no chat
    session, no UI presence. The verdict lands in *captured* via closure."""
    from utils.cloud.cloud_utils import set_user_context, set_tool_capture
    from chat.backend.agent.utils.tool_context_capture import ToolContextCapture
    from chat.backend.agent.agent import Agent
    from chat.backend.agent.db import PostgreSQLClient
    from chat.backend.agent.utils.state import State
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.tools.cloud_tools import get_cloud_tools
    from langchain_core.messages import HumanMessage

    child_session_id = (
        f"{session_id}::recurrence" if session_id else f"recurrence::{incident_id}"
    )

    kickoff = (
        _build_input_block(ctx, incident_id, decision_point)
        + "\n\nBegin now. Investigate briefly, then you MUST end by calling "
        "`submit_correlation_verdict` exactly once. Do not respond with plain "
        "text — every reply must be a tool call, ending with "
        "submit_correlation_verdict."
    )

    state = State(
        question=kickoff,
        messages=[HumanMessage(content=kickoff)],
        user_id=user_id,
        session_id=child_session_id,
        incident_id=str(incident_id),
        incident_start_time=ctx.get("started_at_iso"),
        org_id=org_id,
        is_background=True,  # background-session gating; NOTE: Bitbucket
        # read-only gating additionally needs rca_context.source, so the
        # bitbucket_* bundles are denied by name in EXCLUDED_TOOL_NAMES.
        mode="ask",
        model=ModelConfig.RECURRENCE_AGENT_MODEL,
    )

    # Bind ContextVars BEFORE get_cloud_tools: it reads user context, state
    # (is_background gating) and the tool capture from them.
    set_user_context(
        user_id=user_id,
        session_id=child_session_id,
        provider_preference=None,
        selected_project_id=None,
        state=state,
        mode="ask",
    )
    tool_capture = ToolContextCapture(
        session_id=child_session_id,
        user_id=user_id,
        incident_id=None,  # no incident UI presence for the check's tool calls
        org_id=org_id,
    )
    set_tool_capture(tool_capture)

    # Off-thread: get_cloud_tools does blocking I/O (per-provider connection
    # checks via Vault, MCP tool fetch). Run in a worker thread so the event
    # loop stays responsive and asyncio.wait_for can enforce the wall-clock
    # budget during the build (contextvars propagate into to_thread).
    all_tools = await asyncio.to_thread(get_cloud_tools)
    tools = [
        t for t in all_tools if not _is_excluded_tool(getattr(t, "name", "") or "")
    ]
    logger.info(
        "%s Tools for recurrence check on %s: %d of %d after denylist",
        _LOG_PREFIX, incident_id, len(tools), len(all_tools),
    )
    tools = tools + [
        make_submit_verdict_tool(captured),
    ]

    postgres_client = PostgreSQLClient()
    # NOTE: Agent takes postgres only on this branch — the weaviate RCA-similarity
    # store was removed. Recurrence candidates now come from the Incident Index
    # (injected into the input block) plus the recent-incidents fallback; the
    # agent confirms anchors with the read-only get_incident/list_incidents tools.
    try:
        agent = Agent(
            postgres_client=postgres_client,
        )
        agent.set_tool_capture(tool_capture)
        await agent.agentic_tool_flow(
            state,
            system_prompt_override=_load_prompt(),
            tool_subset=tools,
            max_turns=MAX_TURNS,
        )
    finally:
        try:
            postgres_client.close()
        except Exception:
            logger.exception("%s Failed to close postgres client", _LOG_PREFIX)


def _execute_agent_check(
    *,
    incident_id: str,
    user_id: str,
    session_id: Optional[str],
    ctx: Dict[str, Any],
    decision_point: str,
) -> Tuple[Optional[RecurrenceVerdict], Optional[str]]:
    """Run the agent under the wall-clock budget; return (verdict, reject).

    Re-raises SoftTimeLimitExceeded so the caller's degrade path handles it.
    """
    captured: Dict[str, Any] = {}
    reject: Optional[str] = None
    try:
        asyncio.run(
            asyncio.wait_for(
                _run_agent(
                    incident_id=str(incident_id),
                    user_id=user_id,
                    session_id=session_id,
                    org_id=ctx.get("org_id"),
                    ctx=ctx,
                    decision_point=decision_point,
                    captured=captured,
                ),
                timeout=get_agent_timeout_seconds(),
            )
        )
    except (asyncio.TimeoutError, TimeoutError):
        if "verdict" not in captured:
            reject = REJECT_TIMEOUT
        logger.warning(
            "%s Recurrence check timed out for incident %s (verdict_captured=%s)",
            _LOG_PREFIX, incident_id, "verdict" in captured,
        )
    except Exception as exc:
        if _SoftTimeLimit is not None and isinstance(exc, _SoftTimeLimit):
            # Celery's soft limit fired mid-agent: stop all recurrence work
            # now — the outer handler degrades to standalone so the caller
            # can still notify inside the soft-to-hard margin.
            raise
        if "verdict" not in captured:
            reject = REJECT_ERROR
        logger.exception(
            "%s Recurrence agent failed for incident %s (verdict_captured=%s)",
            _LOG_PREFIX, incident_id, "verdict" in captured,
        )

    verdict: Optional[RecurrenceVerdict] = captured.get("verdict")
    if verdict is None and reject is None:
        reject = REJECT_NO_VERDICT
    return verdict, reject


def _apply_verdict_outcome(
    *,
    incident_id: str,
    user_id: str,
    mode: str,
    decision_point: str,
    verdict: Optional[RecurrenceVerdict],
    reject: Optional[str],
    correlator_score: Optional[float],
    elapsed_ms: int,
    model: Optional[str],
) -> None:
    """Clamp the claim, then fold (live) or persist the verdict row."""
    claimed = verdict.recurrence_of if verdict else None
    reasoning = verdict.reasoning if verdict else None

    accepted: Optional[str] = None
    if reject is None and claimed:
        accepted, reject = _clamp_claimed_id(claimed, str(incident_id), user_id)

    if mode == MODE_LIVE and accepted:
        # Pass the verbatim claim — fold_incident re-validates, resolves the
        # group root itself, and records claimed vs accepted separately.
        result = fold_incident(
            incident_id=str(incident_id),
            user_id=user_id,
            claimed_recurrence_of=claimed,
            reasoning=reasoning or "",
            mode=mode,
            decision_point=decision_point,
            correlator_score=correlator_score,
            elapsed_ms=elapsed_ms,
            model=model,
        )
        logger.info(
            "%s Live verdict for incident %s: folded=%s root=%s reject=%s (%dms)",
            _LOG_PREFIX, incident_id, result.folded, result.root_id,
            result.reject_reason, elapsed_ms,
        )
        return

    if accepted:
        # Same convention the rule correlator uses for its shadow decisions.
        logger.info(
            "[CORRELATION][SHADOW] Would fold incident %s into %s (recurrence agent, %dms)",
            incident_id, accepted, elapsed_ms,
        )
    persist_verdict(
        user_id,
        incident_id=str(incident_id),
        decision_point=decision_point,
        mode=mode,
        claimed_recurrence_of=claimed,
        accepted_recurrence_of=accepted,
        reasoning=reasoning,
        correlator_score=correlator_score,
        folded=False,
        reject_reason=reject,
        elapsed_ms=elapsed_ms,
        model=model,
    )


def run_recurrence_check(
    *,
    incident_id: str,
    user_id: str,
    session_id: Optional[str] = None,
    decision_point: str = "after",
) -> None:
    """Run one recurrence check. Sync, never raises.

    off: no-op. shadow: verdict row only ("Would fold" logged, nothing folds).
    live: an accepted claim folds via fold_incident. Every completed check
    persists exactly one verdict row; every failure path degrades to today's
    behavior.
    """
    started = time.monotonic()
    try:
        mode = get_recurrence_mode()
        if mode == MODE_OFF:
            return

        if get_existing_verdict(incident_id, user_id, decision_point):
            logger.info(
                "%s Verdict already exists for incident %s (%s); skipping re-run",
                _LOG_PREFIX, incident_id, decision_point,
            )
            return

        ctx = _fetch_incident_context(incident_id, user_id)
        if not ctx:
            return

        from chat.backend.agent.llm import ModelConfig

        verdict, reject = _execute_agent_check(
            incident_id=str(incident_id),
            user_id=user_id,
            session_id=session_id,
            ctx=ctx,
            decision_point=decision_point,
        )
        _apply_verdict_outcome(
            incident_id=str(incident_id),
            user_id=user_id,
            mode=mode,
            decision_point=decision_point,
            verdict=verdict,
            reject=reject,
            correlator_score=(ctx.get("hint") or {}).get("score"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            model=ModelConfig.RECURRENCE_AGENT_MODEL,
        )
    except asyncio.CancelledError:
        # A cancellation leaking out of asyncio.run is not a process shutdown
        # signal here — honor the never-raises contract so the caller's
        # completion notification still goes out.
        logger.error(
            "%s Recurrence check cancelled for incident %s; degrading to standalone",
            _LOG_PREFIX, incident_id,
        )
    except Exception as exc:
        if _SoftTimeLimit is not None and isinstance(exc, _SoftTimeLimit):
            logger.error(
                "%s Soft time limit hit during recurrence check for %s; degrading to standalone",
                _LOG_PREFIX, incident_id,
            )
            return
        logger.exception(
            "%s Recurrence check failed for incident %s; degrading to standalone",
            _LOG_PREFIX, incident_id,
        )

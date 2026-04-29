"""Orchestrator nodes: triage, plan, fan_out, synthesize_or_replan, finalize."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from langchain_core.messages import AIMessage
from langgraph.constants import Send
from langgraph.graph import END
from pydantic import BaseModel, Field

from chat.backend.agent.orchestrator.catalog import get_enabled_catalog
from chat.backend.agent.orchestrator.findings_reader import (
    FindingsValidationError,
    read_findings,
)
from chat.backend.agent.orchestrator.state import MainAgentState

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_MAX_REPLAN_ROUNDS = 2

_GLOBAL_DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000000"

_MULTI_AGENT_CONFIG_DEFAULTS: dict[str, Any] = {
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
    "fallback_provider_chain": None,
}

_MULTI_AGENT_CONFIG_COLUMNS = (
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


_PLANNER_SYSTEM_PROMPT = (
    "You are the Aurora multi-agent orchestrator's planner. Given an incident question "
    "and optional org knowledge-base context, decide which sub-agents to fan out in "
    "parallel. The catalog below is INSPIRATION ONLY — you may pick any purpose, even "
    "ones not in the catalog. Each sub-agent must have a focused, distinct purpose. "
    "Hard limit: never emit more than `max_parallel_subagents` sub-agents. Prefer fewer "
    "if the question is narrow. Set `builtin_hint` to a catalog id when one cleanly "
    "fits, else null. Cite any KB hints you used in `memory_hints_used`."
)


class PlannedSubAgent(BaseModel):
    purpose: str
    rationale: str
    builtin_hint: Optional[str] = None
    suggested_skill_focus: list[str] = Field(default_factory=list)


class FanOutPlan(BaseModel):
    selected: list[PlannedSubAgent] = Field(default_factory=list)
    memory_hints_used: list[str] = Field(default_factory=list)
    rationale: str = ""


class TriageDecision(BaseModel):
    complexity: Literal["trivial", "fan_out_warranted"]
    rationale: str
    confidence: float
    suggested_count: int


def _fetch_kb_memory(user_id: Optional[str]) -> str:
    if not user_id:
        return ""
    try:
        from chat.backend.agent.prompt.context_fetchers import (
            build_knowledge_base_memory_segment,
        )

        return build_knowledge_base_memory_segment(user_id) or ""
    except Exception as e:
        logger.warning("[orchestrator:_fetch_kb_memory] %s", e)
        return ""


def _row_to_config(row: tuple) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for idx, col in enumerate(_MULTI_AGENT_CONFIG_COLUMNS):
        cfg[col] = row[idx]
    return cfg


def _load_multi_agent_config(org_id: Optional[str]) -> dict[str, Any]:
    cfg = dict(_MULTI_AGENT_CONFIG_DEFAULTS)
    if not org_id:
        return cfg
    try:
        from utils.db.connection_pool import db_pool

        cols_sql = ", ".join(_MULTI_AGENT_CONFIG_COLUMNS)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_org_id = %s", (org_id,))
                # Single query: org row last (so it overrides) via ORDER BY a synthetic priority.
                cursor.execute(
                    f"SELECT {cols_sql} FROM multi_agent_config "
                    f"WHERE org_id IN (%s, %s) "
                    f"ORDER BY (org_id = %s) ASC",  # global first, org last
                    (_GLOBAL_DEFAULT_ORG_ID, org_id, org_id),
                )
                for row in cursor.fetchall():
                    for k, v in _row_to_config(row).items():
                        if v is not None:
                            cfg[k] = v
    except Exception as e:
        logger.warning("[orchestrator:_load_multi_agent_config] %s", e)
    return cfg


def _get_token_spend_for_incident(
    incident_id: Optional[str], org_id: Optional[str], user_id: Optional[str]
) -> int:
    if not incident_id:
        return 0
    try:
        from chat.backend.agent.llm import get_token_spend

        return int(get_token_spend(incident_id, org_id) or 0)
    except Exception as e:
        logger.warning("[orchestrator:_get_token_spend_for_incident] %s", e)
        return 0


def _get_org_monthly_token_spend(org_id: Optional[str]) -> Optional[int]:
    if not org_id:
        return None
    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_org_id = %s", (org_id,))
                cursor.execute(
                    "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage_tracking "
                    "WHERE org_id = %s AND created_at > NOW() - INTERVAL '30 days'",
                    (org_id,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logger.warning("[orchestrator:_get_org_monthly_token_spend] %s", e)
        return None


def _check_monthly_token_cap(state: MainAgentState) -> tuple[bool, Optional[int], Optional[int]]:
    cfg = state.multi_agent_config or {}
    cap = cfg.get("monthly_token_cap")
    if not cap:
        return (True, None, None)
    try:
        cap_int = int(cap)
    except (TypeError, ValueError):
        return (True, None, None)
    if cap_int <= 0:
        return (True, None, None)

    spent = _get_org_monthly_token_spend(state.org_id)
    if spent is None:
        return (True, None, cap_int)

    if spent >= cap_int:
        return (False, spent, cap_int)

    if spent > int(cap_int * 0.8):
        logger.warning(
            "[MultiAgentRCA:cost] monthly cap 80%%+ used (%d / %d)", spent, cap_int
        )
        try:
            from chat.backend.agent.llm import MONTHLY_CAP_WARNING_FLAG
            existing = list(state.provider_preference or [])
            if MONTHLY_CAP_WARNING_FLAG not in existing:
                existing.append(MONTHLY_CAP_WARNING_FLAG)
            state.provider_preference = existing
        except Exception as e:
            logger.warning("[orchestrator:_check_monthly_token_cap:flag] %s", e)
    return (True, spent, cap_int)


def _check_token_budget(state: MainAgentState) -> tuple[bool, int, int]:
    cfg = state.multi_agent_config or {}
    budget = int(cfg.get("per_rca_token_budget") or _MULTI_AGENT_CONFIG_DEFAULTS["per_rca_token_budget"])
    spent = _get_token_spend_for_incident(state.incident_id, state.org_id, state.user_id)
    return (spent < budget, spent, budget)


def _render_catalog(catalog: dict[str, dict]) -> str:
    rows = []
    for cid, entry in catalog.items():
        rows.append(
            f"- {cid} ({entry.get('ui_label', cid)}): {entry.get('purpose_template', '')}"
        )
    return "\n".join(rows) if rows else "(catalog empty)"


def _mark_incident_multi_agent(incident_id: Optional[str], user_id: Optional[str]) -> None:
    if not incident_id or not user_id:
        return
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, user_id, log_prefix="[orchestrator:_mark_incident_multi_agent]"
                )
                if not org_id:
                    return
                cursor.execute(
                    "UPDATE incidents SET is_multi_agent = TRUE WHERE id = %s",
                    (incident_id,),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[orchestrator:_mark_incident_multi_agent] %s", e)


def _ensure_main_run_row(state: MainAgentState, model_used: Optional[str] = None) -> None:
    if not state.incident_id or not state.session_id or not state.user_id:
        return
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, state.user_id, log_prefix="[orchestrator:_ensure_main_run_row]"
                )
                if not org_id:
                    return
                cursor.execute(
                    "SELECT 1 FROM incident_subagent_runs "
                    "WHERE incident_id = %s AND session_id = %s AND agent_id = %s AND role = 'main' LIMIT 1",
                    (state.incident_id, state.session_id, state.agent_id or "main"),
                )
                if cursor.fetchone():
                    try:
                        cursor.execute(
                            "SELECT 1 FROM incident_subagent_runs "
                            "WHERE incident_id = %s AND role != 'main' LIMIT 1",
                            (state.incident_id,),
                        )
                        if cursor.fetchone():
                            cursor.execute(
                                "UPDATE incidents SET is_multi_agent = TRUE "
                                "WHERE id = %s AND COALESCE(is_multi_agent, FALSE) = FALSE",
                                (state.incident_id,),
                            )
                            conn.commit()
                    except Exception as e:
                        logger.warning(
                            "[orchestrator:_ensure_main_run_row:backfill] %s", e
                        )
                    return
                cursor.execute(
                    """
                    INSERT INTO incident_subagent_runs
                        (incident_id, session_id, agent_id, parent_agent_id, role,
                         delegate_level, purpose, status, model_used, org_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        state.incident_id,
                        state.session_id,
                        state.agent_id or "main",
                        state.parent_agent_id,
                        "main",
                        state.delegate_level or 0,
                        (state.refined_question or state.question or "")[:4000],
                        "running",
                        model_used,
                        org_id,
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[orchestrator:_ensure_main_run_row] %s", e)


def _mark_main_run_succeeded(state: MainAgentState, model_used: Optional[str]) -> None:
    if not state.incident_id or not state.session_id or not state.user_id:
        return
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, state.user_id, log_prefix="[orchestrator:_mark_main_run_succeeded]"
                )
                if not org_id:
                    return
                cursor.execute(
                    """
                    UPDATE incident_subagent_runs
                       SET status = 'succeeded',
                           ended_at = NOW(),
                           model_used = COALESCE(%s, model_used)
                     WHERE incident_id = %s
                       AND session_id = %s
                       AND agent_id = %s
                       AND role = 'main'
                    """,
                    (
                        model_used,
                        state.incident_id,
                        state.session_id,
                        state.agent_id or "main",
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[orchestrator:_mark_main_run_succeeded] %s", e)


def _count_user_connections(user_id: str) -> Optional[int]:
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, user_id, log_prefix="[orchestrator:_count_user_connections]"
                )
                if not org_id:
                    return None
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT provider FROM user_connections WHERE user_id = %s AND status = 'active'
                        UNION
                        SELECT provider FROM user_tokens WHERE user_id = %s
                    ) AS connected
                    """,
                    (user_id, user_id),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
    except Exception as e:
        logger.warning("[orchestrator:_count_user_connections] %s", e)
        return None


def _incident_severity(state: MainAgentState) -> Optional[str]:
    if not state.incident_id or not state.user_id:
        return None
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, state.user_id, log_prefix="[orchestrator:_incident_severity]"
                )
                if not org_id:
                    return None
                cursor.execute(
                    "SELECT severity FROM incidents WHERE id = %s LIMIT 1",
                    (state.incident_id,),
                )
                row = cursor.fetchone()
                if not row or not row[0]:
                    return None
                return str(row[0]).strip().lower()
    except Exception as e:
        logger.warning("[orchestrator:_incident_severity] %s", e)
        return None


def _heuristic_pre_triage(state: MainAgentState) -> Optional[str]:
    try:
        if state.user_id:
            count = _count_user_connections(state.user_id)
            if count is not None and count <= 1:
                logger.info("[orchestrator.triage] heuristic: zero-integration org (count=%d)", count)
                return "trivial"
    except Exception as e:
        logger.warning("[orchestrator:_heuristic_pre_triage:integrations] %s", e)

    try:
        rca_ctx = state.rca_context or {}
        if isinstance(rca_ctx, dict):
            if rca_ctx.get("runbook_url") or rca_ctx.get("runbook_tag"):
                logger.info("[orchestrator.triage] heuristic: runbook present")
                return "trivial"
    except Exception as e:
        logger.warning("[orchestrator:_heuristic_pre_triage:runbook] %s", e)

    if state.incident_id and state.user_id:
        try:
            from utils.auth.stateless_auth import set_rls_context
            from utils.db.connection_pool import db_pool

            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    org_id = set_rls_context(
                        cursor, conn, state.user_id, log_prefix="[orchestrator:_heuristic_pre_triage:fingerprint]"
                    )
                    if org_id:
                        cursor.execute(
                            "SELECT fingerprint FROM incidents WHERE id = %s LIMIT 1",
                            (state.incident_id,),
                        )
                        fp_row = cursor.fetchone()
                        if fp_row and fp_row[0]:
                            cursor.execute(
                                """
                                SELECT 1 FROM incidents i_prior
                                WHERE i_prior.org_id = %s
                                  AND i_prior.fingerprint = %s
                                  AND i_prior.aurora_status = 'resolved'
                                  AND i_prior.resolved_at > NOW() - INTERVAL '24 hours'
                                  AND i_prior.id != %s
                                LIMIT 1
                                """,
                                (org_id, fp_row[0], state.incident_id),
                            )
                            if cursor.fetchone():
                                logger.info(
                                    "[orchestrator.triage] heuristic: fingerprint repeat within 24h"
                                )
                                return "trivial"
        except Exception as e:
            logger.warning("[orchestrator:_heuristic_pre_triage:fingerprint] %s", e)

    try:
        floor_label = str(
            (state.multi_agent_config or {}).get("multi_agent_min_severity", "medium")
        ).strip().lower()
        floor_rank = _SEVERITY_RANK.get(floor_label)
        if floor_rank is not None:
            sev = _incident_severity(state)
            if sev:
                sev_rank = _SEVERITY_RANK.get(sev)
                if sev_rank is not None and sev_rank < floor_rank:
                    logger.info(
                        "[orchestrator.triage] heuristic: severity %s < floor %s",
                        sev,
                        floor_label,
                    )
                    return "trivial"
    except Exception as e:
        logger.warning("[orchestrator:_heuristic_pre_triage:severity] %s", e)

    return None


def _connected_integrations_summary(user_id: Optional[str]) -> str:
    if not user_id:
        return "(unknown)"
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, user_id, log_prefix="[orchestrator:_connected_integrations_summary]"
                )
                if not org_id:
                    return "(unknown)"
                cursor.execute(
                    """
                    SELECT provider FROM user_connections WHERE user_id = %s AND status = 'active'
                    UNION
                    SELECT provider FROM user_tokens WHERE user_id = %s
                    """,
                    (user_id, user_id),
                )
                rows = cursor.fetchall() or []
                providers = sorted({str(r[0]) for r in rows if r and r[0]})
                return ", ".join(providers) if providers else "(none)"
    except Exception as e:
        logger.warning("[orchestrator:_connected_integrations_summary] %s", e)
        return "(unknown)"


async def _triage_llm(state: MainAgentState) -> dict[str, Any]:
    fallback = {
        "complexity": "fan_out_warranted",
        "rationale": "triage_llm_unavailable",
        "suggested_count": 3,
    }
    try:
        from chat.backend.agent.llm import resolve_role_model
        from chat.backend.agent.providers import create_chat_model

        provider, model_id = resolve_role_model("triage", state.user_id, state.org_id)
        if not model_id:
            return fallback

        model_spec = f"{provider}/{model_id}" if provider else model_id
        llm = create_chat_model(model_spec, temperature=0).with_structured_output(TriageDecision)

        question = (state.refined_question or state.question or "").strip() or "(none)"
        integrations = _connected_integrations_summary(state.user_id)
        prompt = (
            "You are a triage classifier for an incident-response agent. Decide whether a "
            "single-agent path is enough ('trivial') or whether parallel sub-agents are "
            "warranted ('fan_out_warranted'). Be conservative: only fan out for incidents "
            "spanning multiple systems or hypotheses.\n\n"
            f"Incident question:\n{question}\n\n"
            f"Connected integrations: {integrations}\n\n"
            "Return complexity, rationale, confidence in [0,1], and suggested_count "
            "(0-5; suggested parallel sub-agents if fanning out)."
        )
        decision: TriageDecision = await llm.ainvoke(prompt)
        return {
            "complexity": decision.complexity,
            "rationale": decision.rationale,
            "suggested_count": int(decision.suggested_count),
        }
    except Exception as e:
        logger.warning("[orchestrator:_triage_llm] %s", e)
        return fallback


async def triage_node(state: MainAgentState) -> dict[str, Any]:
    cfg = _load_multi_agent_config(state.org_id)
    if not state.multi_agent_config:
        state.multi_agent_config = cfg
    kb_text = _fetch_kb_memory(state.user_id)

    pre = _heuristic_pre_triage(state)
    if pre == "trivial":
        logger.info("[orchestrator.triage] complexity=trivial (heuristic)")
        _ensure_main_run_row(state)
        return {"complexity": "trivial", "multi_agent_config": cfg, "kb_memory": kb_text}

    decision = await _triage_llm(state)
    complexity = decision.get("complexity") or "fan_out_warranted"
    logger.info(
        "[orchestrator.triage] complexity=%s (llm) suggested=%s rationale=%s",
        complexity,
        decision.get("suggested_count"),
        (decision.get("rationale") or "")[:160],
    )
    _ensure_main_run_row(state)
    if complexity == "fan_out_warranted" and state.incident_id:
        _mark_incident_multi_agent(state.incident_id, state.user_id)
    return {"complexity": complexity, "multi_agent_config": cfg, "kb_memory": kb_text}


async def plan_node(state: MainAgentState) -> dict[str, Any]:
    from chat.backend.agent.utils.persistence.chat_events import record_event

    max_parallel = int((state.multi_agent_config or {}).get("max_parallel_subagents", 3))
    catalog = get_enabled_catalog(state.org_id or "")
    kb_text = state.kb_memory or ""
    incident_text = (state.refined_question or state.question or "").strip()

    provider: str = ""
    model_id: str = ""
    plan_dict: dict[str, Any] = {"selected": [], "memory_hints_used": [], "rationale": ""}

    try:
        from chat.backend.agent.llm import MONTHLY_CAP_WARNING_FLAG, resolve_role_model
        from chat.backend.agent.providers import create_chat_model

        prefer_cheap = MONTHLY_CAP_WARNING_FLAG in (state.provider_preference or [])
        provider, model_id = resolve_role_model(
            "orchestrator", state.user_id, state.org_id, prefer_cheap=prefer_cheap,
        )
        if not model_id:
            raise RuntimeError("no orchestrator model resolved")

        model_spec = f"{provider}/{model_id}" if provider else model_id
        llm = create_chat_model(model_spec, temperature=0).with_structured_output(FanOutPlan)

        prompt = (
            f"{_PLANNER_SYSTEM_PROMPT}\n\n"
            f"max_parallel_subagents: {max_parallel}\n\n"
            f"Incident question:\n{incident_text or '(none)'}\n\n"
            f"Catalog (inspiration only):\n{_render_catalog(catalog)}\n\n"
            f"KB memory:\n{kb_text or '(none)'}\n"
        )
        plan_obj: FanOutPlan = await llm.ainvoke(prompt)

        selected = list(plan_obj.selected or [])
        if len(selected) > max_parallel:
            logger.warning(
                "[orchestrator.plan] planner emitted %d > max_parallel=%d; truncating",
                len(selected),
                max_parallel,
            )
            selected = selected[:max_parallel]

        plan_dict = {
            "selected": [s.model_dump() for s in selected],
            "memory_hints_used": list(plan_obj.memory_hints_used or []),
            "rationale": plan_obj.rationale or "",
        }
        plan_dict["_model_used"] = f"{provider}/{model_id}" if provider else model_id

        await record_event(
            session_id=state.session_id or "",
            org_id=state.org_id or "",
            type="plan_committed",
            payload={
                "selected": [
                    {
                        "purpose": s["purpose"],
                        "builtin_hint": s.get("builtin_hint"),
                        "rationale": s.get("rationale", ""),
                        "suggested_skill_focus": s.get("suggested_skill_focus", []),
                    }
                    for s in plan_dict["selected"]
                ],
                "rationale": plan_dict["rationale"],
                "memory_hints_used": plan_dict["memory_hints_used"],
                "status": "committed",
            },
            agent_id="main",
        )
        logger.info("[orchestrator.plan] selected=%d sub-agents", len(plan_dict["selected"]))
    except Exception as e:
        logger.warning("[orchestrator:plan] planner failed: %s", e)
        try:
            await record_event(
                session_id=state.session_id or "",
                org_id=state.org_id or "",
                type="plan_committed",
                payload={"selected": [], "status": "failed", "error": str(e)[:500]},
                agent_id="main",
            )
        except Exception as evt_err:
            logger.warning("[orchestrator:plan] failed plan_committed event: %s", evt_err)
        plan_dict = {"selected": [], "memory_hints_used": [], "rationale": "planner_failed"}

    return {"plan": plan_dict}


def _build_subagent_sends(state: MainAgentState) -> list[Send]:
    sends: list[Send] = []
    for entry in (state.plan or {}).get("selected") or []:
        agent_id = f"sub-{uuid.uuid4().hex[:8]}"
        branch_state = {
            "agent_id": agent_id,
            "parent_agent_id": state.agent_id or "main",
            "purpose": entry.get("purpose", ""),
            "suggested_skill_focus": entry.get("suggested_skill_focus", []) or [],
            "incident_summary": (state.refined_question or state.question or "")[:4000],
            "kb_memory": state.kb_memory or "",
            "user_id": state.user_id,
            "org_id": state.org_id,
            "session_id": state.session_id,
            "incident_id": state.incident_id,
            "delegate_level": (state.delegate_level or 0) + 1,
            "tools_used": [],
            "status": "running",
        }
        sends.append(Send("subagent", branch_state))
    logger.info("[orchestrator.fan_out] dispatching %d sub-agent(s)", len(sends))
    return sends


def route_after_plan(state: MainAgentState):
    if state.complexity != "fan_out_warranted":
        return END
    selected = (state.plan or {}).get("selected") or []
    if len(selected) <= 1:
        return END
    return _build_subagent_sends(state)


def route_after_synthesize(state: MainAgentState):
    if (state.next_action or "").lower() == "fan_out":
        selected = (state.plan or {}).get("selected") or []
        if selected:
            return _build_subagent_sends(state)
    return "finalize"


async def _replan_llm(
    state: MainAgentState, prior_findings: list[str]
) -> Optional[dict[str, Any]]:
    try:
        from chat.backend.agent.llm import MONTHLY_CAP_WARNING_FLAG, resolve_role_model
        from chat.backend.agent.providers import create_chat_model

        prefer_cheap = MONTHLY_CAP_WARNING_FLAG in (state.provider_preference or [])
        provider, model_id = resolve_role_model(
            "orchestrator", state.user_id, state.org_id, prefer_cheap=prefer_cheap,
        )
        if not model_id:
            return None

        max_parallel = int((state.multi_agent_config or {}).get("max_parallel_subagents", 3))
        catalog = get_enabled_catalog(state.org_id or "")
        incident_text = (state.refined_question or state.question or "").strip()

        model_spec = f"{provider}/{model_id}" if provider else model_id
        llm = create_chat_model(model_spec, temperature=0).with_structured_output(FanOutPlan)

        prior_text = "\n".join(f"- {s}" for s in prior_findings) or "(none)"
        prompt = (
            f"{_PLANNER_SYSTEM_PROMPT}\n\n"
            f"max_parallel_subagents: {max_parallel}\n\n"
            "This is a REPLAN round. Prior sub-agents returned weak/no high-strength "
            "findings. Propose 1-2 NEW sub-agents that close the gap; do not repeat "
            "purposes already covered.\n\n"
            f"Incident question:\n{incident_text or '(none)'}\n\n"
            f"Prior findings (one line per sub-agent):\n{prior_text}\n\n"
            f"Catalog (inspiration only):\n{_render_catalog(catalog)}\n"
        )
        plan_obj: FanOutPlan = await llm.ainvoke(prompt)

        selected = list(plan_obj.selected or [])[:max_parallel]
        plan_dict = {
            "selected": [s.model_dump() for s in selected],
            "memory_hints_used": list(plan_obj.memory_hints_used or []),
            "rationale": plan_obj.rationale or "",
            "_model_used": model_spec,
        }
        return plan_dict
    except Exception as e:
        logger.warning("[orchestrator:_replan_llm] %s", e)
        return None


def _extract_summary_section(text: str) -> Optional[str]:
    if not text:
        return None
    for marker in ("## Summary", "Summary:"):
        idx = text.find(marker)
        if idx != -1:
            tail = text[idx + len(marker):].lstrip(" :\n")
            stop = tail.find("\n## ")
            if stop != -1:
                tail = tail[:stop]
            return tail.strip() or None
    return text.strip() or None


async def _maybe_rank_and_cite(
    state: MainAgentState,
    findings_for_rank: list[dict[str, Any]],
    summaries: list[str],
) -> Optional[dict[str, Any]]:
    qualifying = [
        f for f in findings_for_rank
        if str(f.get("self_assessed_strength", "")).lower() in ("high", "medium")
    ]
    if len(qualifying) < 2:
        return None

    try:
        from chat.backend.agent.orchestrator.rank_findings import rank_findings
        from chat.backend.agent.utils.persistence.chat_events import record_event

        ranked = await rank_findings(
            findings_for_rank, user_id=state.user_id, org_id=state.org_id
        )
    except Exception as e:
        logger.warning("[orchestrator.synthesize] rank_findings failed: %s", e)
        return None

    if not ranked:
        return None

    confidences = [float(r.get("confidence", 0.5) or 0.5) for r in ranked]
    if confidences and all(abs(c - 0.5) < 1e-6 for c in confidences):
        return None

    ranked_sorted = sorted(
        ranked, key=lambda r: float(r.get("confidence", 0.0) or 0.0), reverse=True
    )
    top = ranked_sorted[0]
    headline_summary = _extract_summary_section(top.get("summary") or "")
    cited_agents: list[str] = []
    ranking_records: list[dict[str, Any]] = []
    for r in ranked_sorted:
        conf = float(r.get("confidence", 0.0) or 0.0)
        rationale = str(r.get("rationale", "") or "")
        agent_id = str(r.get("agent_id", "") or "")
        ranking_records.append(
            {"agent_id": agent_id, "confidence": conf, "rationale": rationale}
        )
        is_cited = conf >= 0.6
        if is_cited and agent_id:
            cited_agents.append(agent_id)
            try:
                await record_event(
                    session_id=state.session_id or "",
                    org_id=state.org_id or "",
                    type="subagent_finished",
                    payload={
                        "agent_id": agent_id,
                        "cited": True,
                        "confidence": conf,
                        "rationale": rationale,
                    },
                    agent_id=agent_id,
                )
            except Exception as e:
                logger.warning("[orchestrator.synthesize] cited event emit failed: %s", e)

    parts: list[str] = []
    if headline_summary:
        parts.append(f"## Headline\n\n> {headline_summary}\n")
    parts.append("\n\n".join(summaries) if summaries else "(no sub-agent findings)")
    final_text = "\n\n".join(parts)

    return {
        "final_text": final_text,
        "cited_agents": cited_agents,
        "ranking": ranking_records,
    }


async def synthesize_or_replan_node(state: MainAgentState) -> dict[str, Any]:
    from chat.backend.agent.utils.persistence.chat_events import record_event

    summaries: list[str] = []
    findings_for_rank: list[dict[str, Any]] = []
    has_high_strength = False
    all_failed_or_low = True
    any_seen = False

    for raw in state.subagent_results or []:
        result = raw if isinstance(raw, dict) else {}
        any_seen = True
        ref = result.get("findings_artifact_ref")
        agent_id = result.get("agent_id", "?")
        sub_status = result.get("status")
        if not ref:
            summaries.append(f"[{agent_id}] no findings artifact (status={sub_status})")
            continue
        try:
            findings = read_findings(ref)
            fm = findings.get("frontmatter") or {}
            section = findings["sections"].get("Summary", "").strip()
            strength = str(fm.get("self_assessed_strength", "")).strip().lower()
            if strength == "high" and section:
                has_high_strength = True
            if strength not in ("low", ""):
                all_failed_or_low = False
            if sub_status not in ("failed", None) and strength != "low":
                all_failed_or_low = False
            summaries.append(f"[{agent_id}] {section}" if section else f"[{agent_id}] (empty summary)")
            findings_for_rank.append(
                {
                    "agent_id": agent_id,
                    "summary": section,
                    "status": sub_status,
                    "self_assessed_strength": strength,
                }
            )
        except FindingsValidationError as e:
            logger.warning("[orchestrator.synthesize] invalid findings for %s: %s", agent_id, e)
            summaries.append(f"[{agent_id}] (invalid findings: {e})")
        except Exception as e:
            logger.exception("[orchestrator.synthesize] read failure for %s: %s", agent_id, e)
            summaries.append(f"[{agent_id}] (read failed: {e})")

    final_text = "\n\n".join(summaries) if summaries else "(no sub-agent findings)"
    cited_payload: Optional[dict[str, Any]] = await _maybe_rank_and_cite(
        state, findings_for_rank, summaries
    )
    if cited_payload and cited_payload.get("final_text"):
        final_text = cited_payload["final_text"]

    max_total = int((state.multi_agent_config or {}).get("max_total_subagents", 5))
    consumed = len(state.subagent_results or [])
    budget_remaining = max(0, max_total - consumed)
    replan_count = int(state.replan_count or 0)

    monthly_ok, monthly_spent, monthly_cap = _check_monthly_token_cap(state)
    if not monthly_ok:
        logger.warning(
            "[orchestrator.synthesize] monthly token cap exceeded spent=%s cap=%s",
            monthly_spent,
            monthly_cap,
        )
        final_text = (
            f"{final_text}\n\n_(synthesis truncated: monthly token cap exceeded — "
            f"spent={monthly_spent} cap={monthly_cap})_"
        )
        try:
            await record_event(
                session_id=state.session_id or "",
                org_id=state.org_id or "",
                type="assistant_failed",
                payload={
                    "reason": "monthly_token_cap_exceeded",
                    "spent": monthly_spent,
                    "cap": monthly_cap,
                },
                agent_id="main",
            )
        except Exception as e:
            logger.warning("[orchestrator.synthesize] monthly cap event emit failed: %s", e)
        return {
            "messages": [AIMessage(content=final_text)],
            "next_action": "finalize",
        }

    within_budget, spent, token_budget = _check_token_budget(state)
    if not within_budget:
        logger.warning(
            "[orchestrator.synthesize] per-incident token budget exceeded spent=%d budget=%d",
            spent,
            token_budget,
        )
        final_text = (
            f"{final_text}\n\n_(synthesis truncated: per-incident token budget exceeded — "
            f"spent={spent} budget={token_budget})_"
        )
        try:
            await record_event(
                session_id=state.session_id or "",
                org_id=state.org_id or "",
                type="assistant_failed",
                payload={
                    "reason": "token_budget_exceeded",
                    "spent": spent,
                    "budget": token_budget,
                },
                agent_id="main",
            )
        except Exception as e:
            logger.warning("[orchestrator.synthesize] budget event emit failed: %s", e)
        return {
            "messages": [AIMessage(content=final_text)],
            "next_action": "finalize",
        }

    next_action = "finalize"
    new_plan: Optional[dict[str, Any]] = None

    if has_high_strength:
        next_action = "finalize"
    elif replan_count >= _MAX_REPLAN_ROUNDS:
        next_action = "finalize"
    elif budget_remaining <= 0:
        next_action = "finalize"
    elif any_seen and all_failed_or_low:
        candidate = await _replan_llm(state, summaries)
        if candidate and (candidate.get("selected") or []):
            new_plan = candidate
            next_action = "fan_out"

    update: dict[str, Any] = {
        "messages": [AIMessage(content=final_text)],
        "next_action": next_action,
    }
    if cited_payload is not None:
        update["cited_artifact"] = {
            "cited_agents": cited_payload.get("cited_agents") or [],
            "ranking": cited_payload.get("ranking") or [],
        }
    if next_action == "fan_out" and new_plan is not None:
        update["plan"] = new_plan
        update["replan_count"] = replan_count + 1
        logger.info(
            "[orchestrator.synthesize] replan round=%d new_subagents=%d",
            replan_count + 1,
            len(new_plan.get("selected") or []),
        )
    else:
        logger.info(
            "[orchestrator.synthesize] finalize (high_strength=%s replan_count=%d budget=%d)",
            has_high_strength,
            replan_count,
            budget_remaining,
        )
    return update


def _persist_cited_artifact(state: MainAgentState) -> None:
    cited = state.cited_artifact or None
    if not cited or not state.incident_id or not state.session_id or not state.user_id:
        return
    try:
        import json as _json

        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, state.user_id, log_prefix="[orchestrator:_persist_cited_artifact]"
                )
                if not org_id:
                    return
                cursor.execute(
                    """
                    UPDATE incident_subagent_runs
                       SET extra_artifact_refs = COALESCE(extra_artifact_refs, '{}'::jsonb)
                                                 || %s::jsonb
                     WHERE incident_id = %s
                       AND session_id = %s
                       AND agent_id = %s
                       AND role = 'main'
                    """,
                    (
                        _json.dumps(
                            {
                                "cited_agents": cited.get("cited_agents") or [],
                                "ranking": cited.get("ranking") or [],
                            }
                        ),
                        state.incident_id,
                        state.session_id,
                        state.agent_id or "main",
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[orchestrator:_persist_cited_artifact] %s", e)


def _persist_chat_messages(state: MainAgentState) -> None:
    """Append the user question + synthesized assistant reply to the legacy
    chat_sessions.messages JSONB so the chat UI surfaces the multi-agent run."""
    if not state.session_id or not state.user_id:
        return
    assistant_text = ""
    for msg in reversed(state.messages or []):
        content = getattr(msg, "content", None)
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if role in ("ai", "assistant") and isinstance(content, str) and content.strip():
            assistant_text = content
            break
    user_text = state.refined_question or state.question or ""
    if not assistant_text and not user_text:
        return
    try:
        import json as _json
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        now_iso = datetime.now(timezone.utc).isoformat()
        new_messages = []
        if user_text:
            new_messages.append({
                "id": str(uuid.uuid4()),
                "sender": "user",
                "text": user_text,
                "timestamp": now_iso,
            })
        if assistant_text:
            new_messages.append({
                "id": str(uuid.uuid4()),
                "sender": "bot",
                "text": assistant_text,
                "timestamp": now_iso,
            })
        if not new_messages:
            return
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(
                    cursor, conn, state.user_id, log_prefix="[orchestrator:_persist_chat_messages]"
                ):
                    return
                cursor.execute(
                    "UPDATE chat_sessions "
                    "SET messages = COALESCE(messages, '[]'::jsonb) || %s::jsonb, "
                    "    status = 'completed', updated_at = NOW() "
                    "WHERE id = %s",
                    (_json.dumps(new_messages), state.session_id),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[orchestrator:_persist_chat_messages] %s", e)


async def finalize_node(state: MainAgentState) -> dict[str, Any]:
    from chat.backend.agent.utils.persistence.chat_events import record_event

    model_used = (state.plan or {}).get("_model_used") if isinstance(state.plan, dict) else None
    _mark_main_run_succeeded(state, model_used)
    _persist_cited_artifact(state)
    _persist_chat_messages(state)

    try:
        await record_event(
            session_id=state.session_id or "",
            org_id=state.org_id or "",
            type="assistant_finalized",
            payload={
                "incident_id": state.incident_id,
                "subagent_count": len(state.subagent_results or []),
                "model_used": model_used,
            },
            agent_id="main",
        )
    except Exception as e:
        logger.warning("[orchestrator:finalize] event emit failed: %s", e)

    logger.info(
        "[orchestrator.finalize] incident=%s subagents=%d",
        state.incident_id,
        len(state.subagent_results or []),
    )
    return {}


__all__ = [
    "FanOutPlan",
    "PlannedSubAgent",
    "TriageDecision",
    "finalize_node",
    "plan_node",
    "route_after_plan",
    "route_after_synthesize",
    "synthesize_or_replan_node",
    "triage_node",
]

"""Celery task for incremental visualization generation."""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import redis

from celery_config import celery_app
from chat.background.visualization_extractor import VisualizationData, VisualizationExtractor
from utils.db.connection_pool import db_pool
from utils.cache.redis_client import get_redis_client
from utils.auth.stateless_auth import set_rls_context
from chat.backend.constants import MAX_TOOL_OUTPUT_CHARS, INFRASTRUCTURE_TOOLS

logger = logging.getLogger(__name__)
_LOG_PREFIX = "[Visualization]"

# Module-level singleton extractor (reuses LLM client across invocations)
_extractor: Optional[VisualizationExtractor] = None

def _get_extractor() -> VisualizationExtractor:
    """Get or create the singleton VisualizationExtractor."""
    global _extractor
    if _extractor is None:
        _extractor = VisualizationExtractor()
        logger.info(f"{_LOG_PREFIX} Created singleton VisualizationExtractor")
    return _extractor


@celery_app.task(
    bind=True,
    max_retries=1,
    name="chat.background.update_visualization",
    time_limit=120,  # Increased from 30s to 120s for LLM processing
    soft_time_limit=100,
)
def update_visualization(
    self,
    incident_id: str,
    user_id: str,
    session_id: str,
    force_full: bool = False,
    tool_calls_json: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate or update visualization for an RCA incident.
    
    Args:
        incident_id: UUID of the incident
        user_id: User performing the RCA
        session_id: Chat session ID
        force_full: If True, process all available context (final viz)
        tool_calls_json: JSON string of recent tool calls to process
    """
    logger.info(f"{_LOG_PREFIX} Starting update for incident {incident_id} (force_full={force_full})")

    # Hook: check if LLM call is allowed
    from utils.hooks import get_hook
    from utils.auth.stateless_auth import get_org_id_for_user
    hook_allowed, hook_message = get_hook("before_llm_call")(get_org_id_for_user(user_id), user_id)
    if not hook_allowed:
        logger.warning(f"{_LOG_PREFIX} Hook blocked for user {user_id}: {hook_message}")
        return {"incident_id": incident_id, "status": "hook_blocked", "error": hook_message}

    try:
        # Get recent tool calls
        if tool_calls_json:
            tool_calls = json.loads(tool_calls_json)
            logger.info(f"{_LOG_PREFIX} Using {len(tool_calls)} tool calls from parameters")
        else:
            tool_calls = _fetch_recent_tool_calls(session_id, user_id, limit=10 if not force_full else 50)
            logger.info(f"{_LOG_PREFIX} Fetched {len(tool_calls)} tool calls from llm_context_history")
        
        if not tool_calls:
            return {"status": "skipped", "reason": "no_tool_calls"}
        
        existing_viz = _fetch_existing_visualization(incident_id, user_id)
        
        extractor = _get_extractor()
        updated_viz = extractor.extract_incremental(
            tool_calls, existing_viz, is_final=force_full,
            user_id=user_id, session_id=session_id,
        )
        
        if not updated_viz.nodes:
            logger.warning(f"{_LOG_PREFIX} No entities extracted for incident {incident_id}")
            return {"status": "skipped", "reason": "no_entities"}
        
        # Post-process: Remove 'investigating' status from final visualization
        if force_full:
            investigating_count = 0
            for node in updated_viz.nodes:
                if node.status == 'investigating':
                    node.status = 'unknown'
                    investigating_count += 1
            
            if investigating_count > 0:
                logger.info(f"{_LOG_PREFIX} Converted {investigating_count} 'investigating' nodes to 'unknown' in final visualization")
        
        validated_json = updated_viz.model_dump_json(indent=2)
        _store_visualization(incident_id, validated_json, user_id)
        _notify_sse_clients(incident_id, updated_viz.version)
        
        logger.info(
            f"{_LOG_PREFIX} Updated incident {incident_id}: "
            f"v{updated_viz.version}, {len(updated_viz.nodes)} nodes, {len(updated_viz.edges)} edges"
        )
        
        return {
            "status": "success",
            "version": updated_viz.version,
            "nodes": len(updated_viz.nodes),
            "edges": len(updated_viz.edges),
        }
    
    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Update failed for incident {incident_id}: {e}")
        return {"status": "error", "error": str(e)}


def _fetch_recent_tool_calls(session_id: str, user_id: str, limit: int = 10) -> List[Dict]:
    """Fetch recent infrastructure tool calls for an RCA session.

    For orchestrator (fanout) RCAs, the parent session's llm_context_history
    is empty — all tool calls live under child session_ids like
    `{parent}::sa_N` / `{parent}::sa_wN_M`. We aggregate from execution_steps
    so the final visualization has the data it needs.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX):
                    return []

                # Parent session's llm_context_history (single-agent path)
                parent_calls: List[Dict] = []
                cursor.execute(
                    "SELECT llm_context_history FROM chat_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    llm_context = row[0]
                    if isinstance(llm_context, str):
                        llm_context = json.loads(llm_context)
                    for msg in llm_context:
                        if isinstance(msg, dict) and msg.get('name') in INFRASTRUCTURE_TOOLS:
                            parent_calls.append({
                                'tool': msg.get('name'),
                                'output': str(msg.get('content', ''))[:MAX_TOOL_OUTPUT_CHARS],
                            })

                # Child sub-agent sessions (orchestrator fanout path).
                # session_id format: `{parent}::{agent_id}` (e.g. `{uuid}::sa_1`,
                # `{uuid}::sa_w2_1`). execution_steps is the canonical store of
                # tool invocations and is indexed on session_id. Bound the query
                # so it doesn't scale with RCA history — fetch the most recent
                # `limit` rows and reverse to chronological order for the
                # visualization extractor.
                child_calls: List[Dict] = []
                # Prefix range scan over the `{parent}::` namespace — index-friendly
                # and immune to wildcards in session_id (LIKE would mismatch on `%`/`_`).
                # Upper bound replaces the final `:` with `;` (next ASCII codepoint),
                # making it the smallest string strictly greater than every `{sid}::*`.
                child_lo = f"{session_id}::"
                child_hi = f"{session_id}:;"
                cursor.execute(
                    """
                    SELECT tool_name, tool_output
                      FROM execution_steps
                     WHERE session_id >= %s AND session_id < %s
                       AND tool_name = ANY(%s)
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (child_lo, child_hi, list(INFRASTRUCTURE_TOOLS), limit),
                )
                for tname, toutput in reversed(cursor.fetchall()):
                    child_calls.append({
                        'tool': tname,
                        'output': str(toutput or '')[:MAX_TOOL_OUTPUT_CHARS],
                    })

        combined = parent_calls + child_calls
        if not combined:
            logger.warning(
                f"{_LOG_PREFIX} No tool calls found for session {session_id} (parent or children)"
            )
            return []

        logger.info(
            f"{_LOG_PREFIX} Fetched {len(parent_calls)} parent + {len(child_calls)} child "
            f"infrastructure tool calls for session {session_id}"
        )
        return combined[-limit:]

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Failed to fetch tool calls: {e}")
        return []


def _fetch_existing_visualization(incident_id: str, user_id: str) -> Optional[VisualizationData]:
    """Fetch current visualization from incidents table."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX):
                    return None
                cursor.execute("""
                    SELECT visualization_code
                    FROM incidents
                    WHERE id = %s
                """, (incident_id,))
                
                row = cursor.fetchone()
        
        if row and row[0]:
            return VisualizationData.model_validate_json(row[0])
        
        return None
    
    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Failed to fetch existing viz: {e}")
        return None


def _store_visualization(incident_id: str, json_str: str, user_id: str):
    """Store updated visualization in database."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX):
                    raise RuntimeError(f"Cannot resolve org_id for user {user_id}")
                cursor.execute("""
                    UPDATE incidents
                    SET visualization_code = %s,
                        visualization_updated_at = %s
                    WHERE id = %s
                """, (json_str, datetime.now(timezone.utc), incident_id))
                conn.commit()
    
    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Failed to store viz: {e}")
        raise


def _notify_sse_clients(incident_id: str, version: int):
    """Notify SSE listeners via Redis pub/sub."""
    try:
        redis_client = get_redis_client()
        if not redis_client:
            logger.warning(f"{_LOG_PREFIX} Redis unavailable, skipping SSE notification")
            return
        
        channel = f"visualization:{incident_id}"
        message = json.dumps({"type": "update", "version": version})
        redis_client.publish(channel, message)
    except Exception as e:
        logger.warning(f"{_LOG_PREFIX} Failed to notify SSE clients: {e}")

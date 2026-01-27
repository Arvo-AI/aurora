"""Celery task for incremental visualization generation."""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
import redis

from celery_config import celery_app
from chat.background.visualization_extractor import VisualizationData, VisualizationExtractor
from utils.db.connection_pool import db_pool

# Constants (avoid circular import with task.py)
MAX_TOOL_OUTPUT_CHARS = 5000
INFRASTRUCTURE_TOOLS = frozenset(['on_prem_kubectl', 'cloud_exec', 'terminal_exec', 'tailscale_ssh'])

logger = logging.getLogger(__name__)


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
    logger.info(f"[Visualization] Starting update for incident {incident_id} (force_full={force_full})")
    
    try:
        # Get recent tool calls
        if tool_calls_json:
            tool_calls = json.loads(tool_calls_json)
            logger.info(f"[Visualization] Using {len(tool_calls)} tool calls from parameters")
        else:
            tool_calls = _fetch_recent_tool_calls(session_id, user_id, limit=10 if not force_full else 50)
            logger.info(f"[Visualization] Fetched {len(tool_calls)} tool calls from state context")
        
        if not tool_calls:
            return {"status": "skipped", "reason": "no_tool_calls"}
        
        existing_viz = _fetch_existing_visualization(incident_id)
        
        extractor = VisualizationExtractor()
        updated_viz = extractor.extract_incremental(tool_calls, existing_viz)
        
        if not updated_viz.nodes:
            logger.warning(f"[Visualization] No entities extracted for incident {incident_id}")
            return {"status": "skipped", "reason": "no_entities"}
        
        validated_json = updated_viz.model_dump_json(indent=2)
        _store_visualization(incident_id, validated_json)
        _notify_sse_clients(incident_id, updated_viz.version)
        
        logger.info(
            f"[Visualization] Updated incident {incident_id}: "
            f"v{updated_viz.version}, {len(updated_viz.nodes)} nodes, {len(updated_viz.edges)} edges"
        )
        
        return {
            "status": "success",
            "version": updated_viz.version,
            "nodes": len(updated_viz.nodes),
            "edges": len(updated_viz.edges),
        }
    
    except Exception as e:
        logger.error(f"[Visualization] Update failed for incident {incident_id}: {e}")
        return {"status": "error", "error": str(e)}


def _fetch_recent_tool_calls(session_id: str, user_id: str, limit: int = 10) -> List[Dict]:
    """Fetch recent tool calls from database (chat_sessions.messages)."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT messages
                    FROM chat_sessions
                    WHERE id = %s
                """, (session_id,))
                
                row = cursor.fetchone()
        
        if not row or not row[0]:
            logger.warning(f"[Visualization] No messages found for session {session_id}")
            return []
        
        messages = row[0]  # Already parsed as list by psycopg2
        tool_calls = []
        
        # Extract tool messages from the messages array
        for msg in messages:
            if isinstance(msg, dict) and 'name' in msg:
                tool_name = msg.get('name', 'unknown')
                if tool_name in INFRASTRUCTURE_TOOLS:
                    tool_calls.append({
                        'tool': tool_name,
                        'output': str(msg.get('content', ''))[:MAX_TOOL_OUTPUT_CHARS],
                    })
        
        logger.info(f"[Visualization] Fetched {len(tool_calls)} tool calls from database for session {session_id}")
        return tool_calls[-limit:] if tool_calls else []
    
    except Exception as e:
        logger.error(f"[Visualization] Failed to fetch tool calls from database: {e}")
        return []


def _fetch_existing_visualization(incident_id: str) -> Optional[VisualizationData]:
    """Fetch current visualization from incidents table."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
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
        logger.error(f"[Visualization] Failed to fetch existing viz: {e}")
        return None


def _store_visualization(incident_id: str, json_str: str):
    """Store updated visualization in database."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE incidents
                    SET visualization_code = %s,
                        visualization_updated_at = %s
                    WHERE id = %s
                """, (json_str, datetime.utcnow(), incident_id))
                conn.commit()
    
    except Exception as e:
        logger.error(f"[Visualization] Failed to store viz: {e}")
        raise


def _notify_sse_clients(incident_id: str, version: int):
    """Notify SSE listeners via Redis pub/sub."""
    redis_client = None
    try:
        redis_client = redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'))
        channel = f"visualization:{incident_id}"
        message = json.dumps({"type": "update", "version": version})
        redis_client.publish(channel, message)
    except Exception as e:
        logger.warning(f"[Visualization] Failed to notify SSE clients: {e}")
    finally:
        if redis_client:
            redis_client.close()

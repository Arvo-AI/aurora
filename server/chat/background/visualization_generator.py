"""Celery task for incremental visualization generation."""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
import redis

from celery_config import celery_app
from chat.backend.agent.llm import LLMManager
from chat.background.visualization_extractor import VisualizationData, VisualizationExtractor
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=1,
    name="chat.background.update_visualization",
    time_limit=30,
)
def update_visualization(
    self,
    incident_id: str,
    user_id: str,
    session_id: str,
    force_full: bool = False
) -> Dict[str, Any]:
    """
    Incrementally update visualization during RCA investigation.
    
    Args:
        incident_id: Incident UUID
        user_id: User ID
        session_id: Chat session UUID
        force_full: If True, analyze full transcript (for final update)
    """
    try:
        recent_messages = _fetch_recent_tool_calls(session_id, user_id, limit=15 if force_full else 10)
        
        if not recent_messages:
            return {"status": "skipped", "reason": "no_tool_calls"}
        
        existing_viz = _fetch_existing_visualization(incident_id)
        
        extractor = VisualizationExtractor(llm_manager=LLMManager())
        updated_viz = extractor.extract_incremental(recent_messages, existing_viz)
        
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
    """Fetch recent tool calls from chat session."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT llm_context_history
                    FROM chat_sessions
                    WHERE id = %s AND user_id = %s
                """, (session_id, user_id))
                
                row = cursor.fetchone()
        
        if not row or not row[0]:
            return []
        
        history = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        
        tool_calls = []
        for msg in history:
            if msg.get('type') == 'tool' and msg.get('content'):
                tool_calls.append({
                    'tool': msg.get('name', 'unknown'),
                    'output': msg.get('content', ''),
                })
        
        return tool_calls[-limit:] if tool_calls else []
    
    except Exception as e:
        logger.error(f"[Visualization] Failed to fetch tool calls: {e}")
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
    try:
        redis_client = redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'))
        channel = f"visualization:{incident_id}"
        message = json.dumps({"type": "update", "version": version})
        redis_client.publish(channel, message)
    
    except Exception as e:
        logger.warning(f"[Visualization] Failed to notify SSE clients: {e}")

"""Celery task for running background chat sessions.

Background chats are triggered by webhooks (Grafana, Datadog) or manually,
and run without a WebSocket connection. They save to the database like
regular chats and appear in the frontend chat history.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from langchain_core.messages import HumanMessage
from utils.cache.redis_client import get_redis_client
from utils.notifications.email_service import get_email_service
from utils.auth.stateless_auth import get_user_email
from utils.flags.feature_flags import is_slack_enabled
from utils.notifications.slack_notification_service import (
    send_slack_investigation_started_notification,
    send_slack_investigation_completed_notification,
)
from utils.db.connection_pool import db_pool


logger = logging.getLogger(__name__)

_RATE_LIMIT_WINDOW_SECONDS = 300  # 5 minute window
_RATE_LIMIT_MAX_REQUESTS = 5  # Max 5 background chats per window

# RCA sources that use rca_context in system prompt
_RCA_SOURCES = {'grafana', 'datadog', 'netdata', 'splunk', 'slack'}

# Initialize Redis client at module load time - fails if Redis is unavailable
_redis_client = get_redis_client()
if _redis_client is None:
    raise RuntimeError("Redis client is unavailable. Check REDIS_URL configuration.")


def is_background_chat_allowed(user_id: str) -> bool:
    """Check if user is allowed to create another background chat using Redis.
    
    Uses Redis INCR with expiration for counter-based rate limiting.
    Allows up to 5 background chats per 5 minute window.
    This prevents alert floods from creating dozens of expensive RCA chats.
    
    Args:
        user_id: The user ID
        
    Returns:
        True if allowed, False if rate limited
    """
    key = f"background_chat_rate_limit:{user_id}"
    
    # Increment counter and get new value
    count = _redis_client.incr(key)
    
    # Set expiration on first request in window
    if count == 1:
        _redis_client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
    
    if count > _RATE_LIMIT_MAX_REQUESTS:
        ttl = _redis_client.ttl(key)
        logger.warning(f"[BackgroundChat] Rate limited user {user_id} - {count}/{_RATE_LIMIT_MAX_REQUESTS} requests, {ttl}s remaining")
        return False
    
    logger.debug(f"[BackgroundChat] User {user_id} request {count}/{_RATE_LIMIT_MAX_REQUESTS}")
    return True


def _get_connected_integrations(user_id: str) -> Dict[str, bool]:
    """Check which integrations are connected for a user."""
    integrations = {
        'splunk': False,
        'github': False,
    }

    try:
        # Check Splunk
        from chat.backend.agent.tools.splunk_tool import is_splunk_connected
        integrations['splunk'] = is_splunk_connected(user_id)
    except Exception as e:
        logger.debug(f"[BackgroundChat] Error checking Splunk: {e}")

    try:
        # Check GitHub
        from utils.auth.stateless_auth import get_credentials_from_db
        github_creds = get_credentials_from_db(user_id, "github")
        integrations['github'] = bool(github_creds and github_creds.get("access_token"))
    except Exception as e:
        logger.debug(f"[BackgroundChat] Error checking GitHub: {e}")

    return integrations


def _build_rca_context(
    user_id: str,
    trigger_metadata: Optional[Dict[str, Any]] = None,
    provider_preference: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build RCA context dict for background chats.

    This context is passed to State and used by prompt_builder to inject
    RCA instructions into the system prompt (not the user message).

    Returns:
        Dict with source, providers, integrations, etc. or None if not an RCA source.
    """
    source = (trigger_metadata or {}).get('source', '').lower()
    if source not in _RCA_SOURCES:
        return None

    logger.info(f"[BackgroundChat] Building RCA context for source: {source}")

    # Get providers - prefer explicit preference, then fetch from DB
    providers = provider_preference
    if not providers:
        try:
            from chat.background.rca_prompt_builder import get_user_providers
            providers = get_user_providers(user_id)
        except Exception as e:
            logger.warning(f"[BackgroundChat] Failed to fetch providers: {e}")
            providers = []

    # Get connected integrations
    integrations = _get_connected_integrations(user_id)

    logger.info(f"[BackgroundChat] User {user_id} has providers: {providers}, integrations: {integrations}")

    return {
        'source': source,
        'providers': providers or [],
        'integrations': integrations,
        'trigger_metadata': trigger_metadata,
    }


@celery_app.task(
    bind=True, 
    name="chat.background.run_background_chat",
    time_limit=900,  # Hard timeout: 15 minutes (task killed)
    soft_time_limit=900  # Soft timeout: 15 minutes (exception raised, allows cleanup)
)
def run_background_chat(
    self,
    user_id: str,
    session_id: str,
    initial_message: str,
    trigger_metadata: Optional[Dict[str, Any]] = None,
    provider_preference: Optional[List[str]] = None,
    incident_id: Optional[str] = None,
    send_notifications: bool = True,
    mode: str = "ask",
) -> Dict[str, Any]:
    """Run a chat session in the background without WebSocket.

    This task creates a complete chat workflow that:
    - Uses the specified mode (default "ask" for read-only, "agent" for execution)
    - Sets is_background=True to skip confirmations and user questions
    - Saves all messages to the database (same as regular chats)
    - Appears in the frontend chat history
    - Times out after 15 minutes to prevent hanging indefinitely

    Args:
        user_id: The user ID to run the chat for
        session_id: The chat session ID (should be pre-created or will be auto-created)
        initial_message: The prompt/question to send to the agent
        trigger_metadata: Optional metadata about what triggered this chat
            e.g., {"source": "grafana", "alert_id": "abc123"}
        provider_preference: Cloud providers to use, defaults to user's configured providers
        mode: Chat mode - "ask" for read-only (default), "agent" for execution

    Returns:
        Dict with session_id, status, and any error information
    """
    from celery.exceptions import SoftTimeLimitExceeded
    
    logger.info(f"[BackgroundChat] Starting for user {user_id}, session {session_id}")
    logger.info(f"[BackgroundChat] Trigger: {trigger_metadata}")
    
    completed_successfully = False
    
    try:
        # Link session to incident if provided
        if incident_id:
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE incidents SET aurora_chat_session_id = %s WHERE id = %s",
                            (session_id, incident_id)
                        )
                        conn.commit()
                        logger.info(f"[BackgroundChat] Linked session {session_id} to incident {incident_id}")
                    
                    # Set incident aurora_status to running
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE incidents SET aurora_status = %s, updated_at = %s WHERE id = %s",
                            ("running", datetime.now(), incident_id)
                        )
                        conn.commit()
                        logger.info(f"[BackgroundChat] Set incident {incident_id} aurora_status to 'running' at start of RCA")
                    
                    # Send investigation started notifications (if enabled)
                    # Skip notifications if explicitly disabled (e.g., for Slack @mentions)
                    # For email: need both general notifications AND start notifications enabled
                    # For Slack: only need general notifications (no separate start preference since start message is overwritten by end message)
                    email_general_enabled = _is_rca_email_notification_enabled(user_id)
                    email_start_enabled = _is_rca_email_start_notification_enabled(user_id)
                    email_start_notification_enabled = email_general_enabled and email_start_enabled
                    
                    slack_notification_enabled = is_slack_enabled() and _has_slack_connected(user_id)
                    
                    if send_notifications and (email_start_notification_enabled or slack_notification_enabled):
                        _send_rca_notification(user_id, incident_id, 'started', 
                            email_enabled=email_start_notification_enabled,
                            slack_enabled=slack_notification_enabled
                        )
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to link session to incident: {e}")
        
        # Run the async workflow in the sync Celery context
        logger.info(f"[BackgroundChat] Starting workflow execution for session {session_id}, incident {incident_id}")
        try:
            result = asyncio.run(_execute_background_chat(
                user_id=user_id,
                session_id=session_id,
                initial_message=initial_message,
                trigger_metadata=trigger_metadata,
                provider_preference=provider_preference,
                incident_id=incident_id,
                mode=mode,
            ))
            pass
        except Exception as e:
            logger.error(f"[BackgroundChat] Exception in asyncio.run(_execute_background_chat): {e}", exc_info=True)
            raise
        
        logger.info(f"[BackgroundChat] Workflow execution completed for session {session_id}")
        
        # Update session status to completed
        _update_session_status(session_id, "completed")
        
        # Update incident status to analyzed if incident_id provided
        if incident_id:
            _update_incident_status(incident_id, "analyzed")
            _update_incident_aurora_status(incident_id, "complete")
            
            # Determine severity from RCA if currently unknown
            try:
                _determine_severity_from_rca(incident_id, session_id)
            except (ImportError, ModuleNotFoundError) as e:
                error_msg = str(e)
                if 'langchain.schema' in error_msg or 'langchain_schema' in error_msg.lower():
                    logger.warning(
                        f"[BackgroundChat] Skipping severity determination due to langchain.schema compatibility issue. "
                        f"This is a known issue with langchain_openai 1.1.7 and langchain 1.2.6."
                    )
                else:
                    logger.error(f"[BackgroundChat] Failed to determine severity (import error): {e}")
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to determine severity: {e}")
            
            # Regenerate incident summary now that RCA chat has completed
            try:
                from chat.background.summarization import generate_incident_summary_from_chat
                generate_incident_summary_from_chat.delay(
                    incident_id=incident_id,
                    user_id=user_id,
                    session_id=session_id,
                )
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to enqueue post-RCA summarization for incident {incident_id}: {e}")
            
            # Generate final complete visualization
            try:
                # Extract tool calls from state to pass to final viz
                tool_calls_for_viz = []
                try:
                    from chat.backend.agent.tools.cloud_tools import get_state_context
                    state = get_state_context()
                    if state and hasattr(state, 'messages'):
                        for msg in state.messages:
                            if hasattr(msg, '__class__') and 'ToolMessage' in msg.__class__.__name__:
                                tool_name = getattr(msg, 'name', 'unknown')
                                # Only include infrastructure tools
                                if tool_name in ['on_prem_kubectl', 'cloud_exec', 'gcp_compute', 'aws_ec2', 'azure_vm']:
                                    tool_calls_for_viz.append({
                                        'tool': tool_name,
                                        'output': str(getattr(msg, 'content', ''))[:5000],
                                    })
                        logger.info(f"[BackgroundChat] Extracted {len(tool_calls_for_viz)} tool calls for final visualization")
                except Exception as extract_err:
                    logger.warning(f"[BackgroundChat] Failed to extract tool calls for final viz: {extract_err}")
                
                from chat.background.visualization_generator import update_visualization
                import json
                update_visualization.apply_async(
                    kwargs={
                        'incident_id': incident_id,
                        'user_id': user_id,
                        'session_id': session_id,
                        'force_full': True,
                        'tool_calls_json': json.dumps(tool_calls_for_viz) if tool_calls_for_viz else None
                    },
                    countdown=5
                )
                logger.info(f"[BackgroundChat] Queued final visualization for incident {incident_id}")
            except Exception as e:
                logger.warning(f"[BackgroundChat] Failed to queue final visualization: {e}")
        
        # Send response back to Slack if this was triggered from Slack
        if trigger_metadata and trigger_metadata.get('source') in ['slack', 'slack_button']:
            try:
                _send_response_to_slack(user_id, session_id, trigger_metadata)
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to send response to Slack: {e}", exc_info=True)
        
        completed_successfully = True
        logger.info(f"[BackgroundChat] Completed for session {session_id}")
        return result
    
    except SoftTimeLimitExceeded:
        logger.error(f"[BackgroundChat] Timeout after 15 minutes for session {session_id}")
        _update_session_status(session_id, "failed")
        if incident_id:
            _update_incident_aurora_status(incident_id, "error")
        return {
            "session_id": session_id,
            "status": "failed",
            "error": "Background chat exceeded 15 minute timeout",
        }
        
    except Exception as e:
        logger.exception(f"[BackgroundChat] Failed for session {session_id}: {e}")
        _update_session_status(session_id, "failed")
        if incident_id:
            _update_incident_aurora_status(incident_id, "error")
        return {
            "session_id": session_id,
            "status": "failed",
            "error": str(e),
        }
    
    finally:
        # Safety net: ensure session is never left in in_progress state
        if not completed_successfully:
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE chat_sessions SET status = 'failed', updated_at = %s WHERE id = %s AND status = 'in_progress'",
                            (datetime.now(), session_id)
                        )
                        if cursor.rowcount > 0:
                            conn.commit()
                            logger.warning(f"[BackgroundChat] Finally block marked session {session_id} as failed")
            except Exception as cleanup_err:
                logger.error(f"[BackgroundChat] Failed to cleanup session {session_id}: {cleanup_err}")


async def _execute_background_chat(
    user_id: str,
    session_id: str,
    initial_message: str,
    trigger_metadata: Optional[Dict[str, Any]] = None,
    provider_preference: Optional[List[str]] = None,
    incident_id: Optional[str] = None,
    mode: str = "ask",
) -> Dict[str, Any]:
    """Execute the background chat workflow asynchronously.

    This function mirrors the workflow setup in main_chatbot.py but:
    - Uses BackgroundWebSocket instead of a real WebSocket
    - Uses the specified mode ("ask" for read-only, "agent" for execution)
    - Sets is_background=True to skip confirmations
    """
    # Import here to avoid circular dependencies
    from chat.backend.agent.agent import Agent
    from chat.backend.agent.db import PostgreSQLClient
    from chat.backend.agent.utils.state import State
    from chat.backend.agent.workflow import Workflow
    from chat.backend.agent.weaviate_client import WeaviateClient
    from chat.backend.agent.tools.cloud_tools import set_user_context
    from chat.background.background_websocket import BackgroundWebSocket
    from main_chatbot import process_workflow_async
    
    weaviate_client = None
    
    try:
        # Initialize clients (same as handle_connection in main_chatbot.py)
        postgres_client = PostgreSQLClient()
        weaviate_client = WeaviateClient(postgres_client)
        
        # Create background websocket (no-op, just discards messages)
        background_ws = BackgroundWebSocket()
        
        # Create agent WITHOUT websocket_sender - tools will skip WebSocket messages
        # Use reasonable ctx_len for RCAs - need enough history to build on previous tool calls
        # But not too high to avoid context length errors (Azure has 128K limit)
        # 15 is a good balance - allows agent to see its investigation progress while staying within limits
        agent = Agent(
            weaviate_client=weaviate_client,
            postgres_client=postgres_client,
            websocket_sender=None,
            event_loop=None,
            ctx_len=15,  # Reasonable history for RCAs - allows agent to see investigation progress
        )
        logger.info(f"[BackgroundChat] Created agent with ctx_len=15 (no WebSocket)")
        
        # Create workflow for this session
        wf = Workflow(agent, session_id)
        logger.info(f"[BackgroundChat] Created workflow for session {session_id}")
        
        # Build RCA context for system prompt (NOT added to user message)
        rca_context = _build_rca_context(
            user_id=user_id,
            trigger_metadata=trigger_metadata,
            provider_preference=provider_preference,
        )
        if rca_context:
            logger.info(f"[BackgroundChat] Built RCA context: source={rca_context.get('source')}, providers={rca_context.get('providers')}")

        # Create the initial message (kept simple - user sees this)
        human_message = HumanMessage(content=initial_message)

        # Import centralized model config
        from chat.backend.agent.llm import ModelConfig

        # Create state with is_background=True and rca_context for system prompt
        # Use centralized model configuration for RCA with provider mode awareness
        state = State(
            user_id=user_id,
            session_id=session_id,
            incident_id=incident_id,
            provider_preference=provider_preference,
            selected_project_id=None,
            messages=[human_message],
            question=initial_message,
            model=ModelConfig.RCA_MODEL,
            mode=mode,
            is_background=True,  # Key flag for background behavior
            rca_context=rca_context,  # RCA context for prompt_builder
        )
        logger.info(f"[BackgroundChat] Created state with is_background=True, mode={mode}, model={state.model}, rca_context={'set' if rca_context else 'None'}")
        
        # Set user context for tools (AFTER state is created so we can pass it)
        set_user_context(
            user_id=user_id,
            session_id=session_id,
            provider_preference=provider_preference,
            selected_project_id=None,
            mode=mode,
            state=state,  # Pass state so incident_id is available in context
        )
        logger.info(f"[BackgroundChat] Set user context with mode={mode}, incident_id={incident_id}")
        
        # Set UI state (preserve triggerMetadata so it persists when workflow saves)
        wf._ui_state = {
            "selectedMode": mode,
            "selectedProviders": provider_preference or [],
            "isBackground": True,
        }
        if trigger_metadata:
            wf._trigger_metadata = trigger_metadata
            wf._ui_state["triggerMetadata"] = trigger_metadata
        
        # Run the workflow - this is the same function used by regular chats
        await process_workflow_async(wf, state, background_ws, user_id, incident_id=incident_id)
        
        # CRITICAL: Wait for any ongoing tool calls to complete before marking as done
        # The workflow stream might complete, but tool calls could still be running
        if hasattr(wf, '_wait_for_ongoing_tool_calls'):
            await wf._wait_for_ongoing_tool_calls()
        
        if incident_id:
            # Check if status was already set to complete (shouldn't happen, but log if it does)
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT aurora_status FROM incidents WHERE id = %s", (incident_id,))
                    row = cursor.fetchone()
                    if row and row[0] == 'complete':
                        logger.error(f"[BackgroundChat] ⚠️ WARNING: Incident {incident_id} aurora_status is already 'complete' before we set it! This indicates a race condition.")
        
        logger.info(f"[BackgroundChat] Workflow execution completed - all streams and tool calls finished")
        
        return {
            "session_id": session_id,
            "status": "completed",
            "trigger_metadata": trigger_metadata,
        }
        
    except Exception as e:
        logger.exception(f"[BackgroundChat] Error during execution: {e}")
        raise
        
    finally:
        # Clean up async save queue to allow asyncio.run() to return
        try:
            from chat.backend.agent.utils.persistence.context_manager import ContextManager
            if hasattr(ContextManager, '_instance') and hasattr(ContextManager._instance, 'async_queue'):
                await ContextManager._instance.async_queue.stop()
        except Exception as e:
            logger.error(f"[BackgroundChat] Failed to stop async save queue - potential resource leak: {e}")
        
        # Clean up weaviate client
        if weaviate_client:
            try:
                weaviate_client.close()
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to close weaviate client - potential connection leak: {e}")


def _update_session_status(session_id: str, status: str) -> None:
    """Update the status of a chat session.
    
    Args:
        session_id: The chat session ID
        status: New status ('in_progress', 'completed', 'failed', 'active')
    """ 
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE chat_sessions SET status = %s, updated_at = %s WHERE id = %s",
                    (status, datetime.now(), session_id)
                )
                rows_updated = cursor.rowcount
            conn.commit()
            logger.info(f"[BackgroundChat] Updated session {session_id} status to '{status}' (rows={rows_updated})")
    except Exception as e:
        logger.error(f"[BackgroundChat] Failed to update session {session_id} status to '{status}': {e}")


def _update_incident_aurora_status(incident_id: str, aurora_status: str) -> None:
    """Update incident aurora_status (running/complete/error).
    
    Args:
        incident_id: The incident ID
        aurora_status: New status ('running', 'complete', 'error')
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE incidents SET aurora_status = %s, updated_at = %s WHERE id = %s",
                    (aurora_status, datetime.now(), incident_id)
                )
            conn.commit()
            logger.info(f"[BackgroundChat] Set incident {incident_id} aurora_status to '{aurora_status}'")
    except Exception as e:
        logger.error(f"[BackgroundChat] Failed to update aurora_status: {e}")


def _determine_severity_from_rca(incident_id: str, session_id: str) -> None:
    """Determine severity from RCA chat if currently unknown."""
    try:
        # Use LLMManager which already successfully uses ChatOpenAI elsewhere in the codebase
        from chat.backend.agent.llm import LLMManager
    except ImportError as ie:
        logger.error(f"[BackgroundChat] Failed to import LLMManager: {ie}")
        return
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT severity FROM incidents WHERE id = %s", (incident_id,))
                row = cursor.fetchone()
                if not row or row[0] not in (None, 'unknown'):
                    return  # Severity already set
                
                cursor.execute("SELECT messages FROM chat_sessions WHERE id = %s", (session_id,))
                msg_row = cursor.fetchone()
                if not msg_row or not msg_row[0]:
                    return
                
                messages = json.loads(msg_row[0]) if isinstance(msg_row[0], str) else msg_row[0]
                transcript = "\n".join([f"{m.get('sender', 'unknown')}: {m.get('text', '')[:200]}" for m in messages[:10] if m.get('text')])
                
                try:
                    # Use LLMManager which creates ChatOpenAI instances successfully
                    # This avoids the langchain.schema import issue by using the same code path as the rest of the app
                    llm_manager = LLMManager()
                    model = llm_manager._get_or_create_model("anthropic/claude-sonnet-4.5")
                    
                    # Set temperature to 0 for deterministic severity assessment
                    original_temp = model.temperature
                    model.temperature = 0
                    
                    try:
                        prompt = f"""You are assessing the operational severity of an incident based on its investigation.

Severity levels:
- critical: Production outage, service unavailable, data loss, or security breach affecting customers
- high: Degraded service performance, partial outage, or significant impact to user experience
- medium: Performance issues, minor degradation, or non-customer-facing problems
- low: Informational alerts, monitoring tests, or no actual operational impact detected

Assess based ONLY on actual operational impact found during investigation, not alert keywords or titles.

Investigation transcript:
{transcript}

Respond with ONLY ONE WORD: critical, high, medium, or low"""
                        response = model.invoke([HumanMessage(content=prompt)])
                    finally:
                        # Restore original temperature
                        model.temperature = original_temp
                except (ImportError, ModuleNotFoundError) as ie:
                    # Catch any import errors - should not happen with LLMManager but just in case
                    error_msg = str(ie)
                    if 'langchain.schema' in error_msg or 'langchain_schema' in error_msg.lower():
                        logger.warning(
                            f"[BackgroundChat] Skipping severity determination due to langchain.schema compatibility issue. "
                            f"Error: {error_msg}"
                        )
                    else:
                        logger.error(f"[BackgroundChat] Import/Module error: {ie}")
                    return
                except Exception as llm_error:
                    logger.error(f"[BackgroundChat] Error calling LLM for severity determination: {llm_error}")
                    return
                
                # Safely extract content from response - handle both AIMessage and dict responses
                # Also handle Gemini thinking model responses (list with thinking/text blocks)
                if hasattr(response, 'content'):
                    content = response.content
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict):
                                part_type = part.get("type", "")
                                if part_type not in ("thinking", "reasoning"):
                                    text = part.get("text", "")
                                    if text:
                                        text_parts.append(str(text))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        severity_raw = "".join(text_parts).strip().lower()
                    else:
                        severity_raw = str(content).strip().lower()
                elif isinstance(response, dict):
                    severity_raw = str(response.get('content', '')).strip().lower()
                else:
                    severity_raw = str(response).strip().lower()
                
                # Extract severity level from response (handles cases where LLM wraps answer)
                severity = None
                for level in ('critical', 'high', 'medium', 'low'):
                    if level in severity_raw:
                        severity = level
                        break
                
                if severity:
                    cursor.execute("UPDATE incidents SET severity = %s, updated_at = %s WHERE id = %s", (severity, datetime.now(), incident_id))
                    conn.commit()
                    logger.info(f"[BackgroundChat] Set incident {incident_id} severity to '{severity}' from RCA")
                else:
                    logger.warning(f"[BackgroundChat] Could not parse severity from LLM response: {severity_raw}")
    except Exception as e:
        logger.error(f"[BackgroundChat] Failed to determine severity: {e}")


def _update_incident_status(incident_id: str, status: str) -> None:
    """Update incident status when RCA completes.
    
    Args:
        incident_id: The incident ID
        status: New status ('investigating', 'analyzed')
    """
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE incidents 
                    SET status = %s, 
                        analyzed_at = CASE WHEN %s = 'analyzed' THEN %s ELSE analyzed_at END,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (status, status, datetime.now(), datetime.now(), incident_id)
                )
                rows_updated = cursor.rowcount
            conn.commit()
            logger.info(f"[BackgroundChat] Updated incident {incident_id} status to '{status}' (rows={rows_updated})")
    except Exception as e:
        logger.error(f"[BackgroundChat] Failed to update incident {incident_id} status to '{status}': {e}")


def _is_rca_email_notification_enabled(user_id: str) -> bool:
    """Check if user has RCA email notifications enabled.
    
    Args:
        user_id: The user ID
        
    Returns:
        True if email notifications are enabled, False otherwise
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT preference_value 
                    FROM user_preferences 
                    WHERE user_id = %s AND preference_key = 'rca_email_notifications'
                    """,
                    (user_id,)
                )
                result = cursor.fetchone()
                if result and result[0] is not None:
                    value = result[0]
                    # preference_value is JSONB, stored as boolean from frontend
                    if isinstance(value, bool):
                        return value
                    # Unexpected format - log and default to False
                    logger.warning(f"[EmailNotification] Unexpected preference format for rca_email_notifications: {type(value).__name__}, expected bool")
        
        # Default: notifications disabled (opt-in)
        return False
        
    except Exception as e:
        logger.error(f"[EmailNotification] Error checking notification preference: {e}")
        return False


def _has_slack_connected(user_id: str) -> bool:
    """Check if user has Slack connected."""
    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        client = get_slack_client_for_user(user_id)
        return client is not None
    except Exception as e:
        logger.error(f"[SlackNotification] Error checking Slack connection: {e}")
        return False


def _is_rca_email_start_notification_enabled(user_id: str) -> bool:
    """Check if user has RCA investigation start email notifications enabled.
    
    This is a separate preference from the general RCA email notifications, allowing users
    to receive completion emails without being notified when investigations start.
    
    Args:
        user_id: The user ID
        
    Returns:
        True if email start notifications are enabled, False otherwise (default: False)
    """
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT preference_value 
                    FROM user_preferences 
                    WHERE user_id = %s AND preference_key = 'rca_email_start_notifications'
                    """,
                    (user_id,)
                )
                result = cursor.fetchone()
                if result and result[0] is not None:
                    value = result[0]
                    # preference_value is JSONB, stored as boolean from frontend
                    if isinstance(value, bool):
                        return value
                    # Unexpected format - log and default to False
                    logger.warning(f"[EmailNotification] Unexpected preference format for rca_email_start_notifications: {type(value).__name__}, expected bool")
        
        # Default: start notifications DISABLED (opt-in)
        return False
        
    except Exception as e:
        logger.error(f"[EmailNotification] Error checking start notification preference: {e}")
        return False


def _get_incident_data(incident_id: str) -> Optional[Dict[str, Any]]:
    """Fetch incident data from database.
    
    Args:
        incident_id: The incident UUID
        
    Returns:
        Dictionary with incident data or None if not found
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, user_id, source_type, status, severity, alert_title, 
                           alert_service, aurora_status, aurora_summary, started_at, 
                           analyzed_at, created_at, slack_message_ts
                    FROM incidents 
                    WHERE id = %s
                    """,
                    (incident_id,)
                )
                result = cursor.fetchone()
                if result:
                    return {
                        'incident_id': str(result[0]),
                        'user_id': result[1],
                        'source_type': result[2],
                        'status': result[3],
                        'severity': result[4] or 'unknown',
                        'alert_title': result[5] or 'Unknown Alert',
                        'service': result[6] or 'unknown',
                        'aurora_status': result[7],
                        'aurora_summary': result[8],
                        'started_at': result[9],
                        'analyzed_at': result[10],
                        'created_at': result[11],
                        'slack_message_ts': result[12] if len(result) > 12 else None,
                    }
        
        return None
        
    except Exception as e:
        logger.error(f"[EmailNotification] Error fetching incident data: {e}")
        return None


def _send_rca_notification(user_id: str, incident_id: str, event_type: str, email_enabled: bool = False, slack_enabled: bool = False, session_id: Optional[str] = None) -> None:
    """Send RCA email and Slack notifications.
    
    Args:
        user_id: The user ID
        incident_id: The incident UUID
        event_type: 'started' or 'completed'
        email_enabled: Whether to send email notifications
        slack_enabled: Whether to send Slack notifications
        session_id: Optional chat session ID (used to extract last message for 'completed' notifications)
    """
    # Get incident data (needed for both email and Slack)
    incident_data = _get_incident_data(incident_id)
    if not incident_data:
        logger.error(f"[RCANotification] Incident {incident_id} not found")
        return
    
    # For completed notifications, extract the summary section from last message if not already present
    if event_type == 'completed' and session_id and not incident_data.get('aurora_summary'):
        try:
            from utils.db.connection_pool import db_pool
            from routes.slack.slack_events_helpers import extract_summary_section
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT messages FROM chat_sessions WHERE id = %s AND user_id = %s",
                        (session_id, user_id)
                    )
                    row = cursor.fetchone()
                    
                    if row and row[0]:
                        messages = row[0]
                        if isinstance(messages, str):
                            messages = json.loads(messages)
                        
                        # Find the last assistant/bot message
                        for msg in reversed(messages):
                            if msg.get('sender') in ('bot', 'assistant'):
                                last_message = msg.get('text') or msg.get('content')
                                if last_message:
                                    # Extract just the summary section (before Next Steps)
                                    summary_only = extract_summary_section(last_message)
                                    incident_data['aurora_summary'] = summary_only
                                    logger.info(f"[RCANotification] Extracted summary section for incident {incident_id} ({len(summary_only)} chars)")
                                break
        except Exception as e:
            logger.warning(f"[RCANotification] Failed to extract summary for incident {incident_id}: {e}")
    
    # --- EMAIL NOTIFICATIONS ---
    if email_enabled:
        try:
            # Get primary email
            user_email = get_user_email(user_id)
            if not user_email:
                logger.warning(f"[EmailNotification] No email found for user {user_id}")
            else:
                # Get all verified additional emails
                additional_emails = []
                try:
                    with db_pool.get_admin_connection() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute(
                                """
                                SELECT email FROM rca_notification_emails
                                WHERE user_id = %s AND is_verified = TRUE AND is_enabled = TRUE
                                ORDER BY verified_at ASC
                                """,
                                (user_id,)
                            )
                            rows = cursor.fetchall()
                            additional_emails = [row[0] for row in rows]
                except Exception as e:
                    logger.error(f"[EmailNotification] Failed to fetch additional emails for user {user_id}: {e}")
                
                # Combine all recipient emails
                all_emails = [user_email] + additional_emails
                logger.info(f"[EmailNotification] Sending {event_type} notification to {len(all_emails)} email(s): {', '.join(all_emails)}")
                
                # Send appropriate email to all recipients
                email_service = get_email_service()
                
                success_count = 0
                for recipient_email in all_emails:
                    try:
                        if event_type == 'started':
                            success = email_service.send_investigation_started_email(recipient_email, incident_data)
                            if success:
                                success_count += 1
                                logger.info(f"[EmailNotification] Sent 'started' email to {recipient_email} for incident {incident_id}")
                            else:
                                logger.warning(f"[EmailNotification] Failed to send 'started' email to {recipient_email}")
                        elif event_type == 'completed':
                            success = email_service.send_investigation_completed_email(recipient_email, incident_data)
                            if success:
                                success_count += 1
                                logger.info(f"[EmailNotification] Sent 'completed' email to {recipient_email} for incident {incident_id}")
                            else:
                                logger.warning(f"[EmailNotification] Failed to send 'completed' email to {recipient_email}")
                    except Exception as e:
                        logger.error(f"[EmailNotification] Error sending to {recipient_email}: {e}")
                
                logger.info(f"[EmailNotification] Successfully sent {success_count}/{len(all_emails)} {event_type} notifications for incident {incident_id}")
        except Exception as e:
            # Don't fail if email fails
            logger.error(f"[EmailNotification] Failed to send {event_type} notification: {e}")
    
    # --- SLACK NOTIFICATIONS ---
    if slack_enabled:
        try:
            if event_type == 'started':
                send_slack_investigation_started_notification(user_id, incident_data)
            elif event_type == 'completed':
                send_slack_investigation_completed_notification(user_id, incident_data)
        except Exception as e:
            # Don't fail if Slack fails
            logger.error(f"[SlackNotification] Failed to send {event_type} notification: {e}", exc_info=True)


def _send_response_to_slack(user_id: str, session_id: str, trigger_metadata: Dict[str, Any]) -> None:
    """Send Aurora's response back to the Slack channel after background chat completes."""
    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        from routes.slack.slack_events_helpers import format_response_for_slack
        
        channel = trigger_metadata.get('channel')
        thread_ts = trigger_metadata.get('thread_ts')
        thinking_message_ts = trigger_metadata.get('thinking_message_ts')
        source = trigger_metadata.get('source')
        
        if not channel:
            logger.warning(f"[BackgroundChat] No Slack channel in trigger_metadata for session {session_id}")
            return
        
        # Get the last assistant message from the chat session
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s AND user_id = %s",
                    (session_id, user_id)
                )
                row = cursor.fetchone()
                
                if not row or not row[0]:
                    logger.warning(f"[BackgroundChat] No messages found in session {session_id}")
                    return
                
                messages = row[0]
                if isinstance(messages, str):
                    import json
                    messages = json.loads(messages)
                
                # Find the last assistant/bot message
                last_assistant_message = None
                for msg in reversed(messages):
                    if msg.get('sender') in ('bot', 'assistant'):
                        last_assistant_message = msg.get('text') or msg.get('content')
                        break
                
                if not last_assistant_message:
                    logger.warning(f"[BackgroundChat] No assistant message found in session {session_id}")
                    return
        
        # Format the response for Slack (markdown conversion, length limits, etc.)
        formatted_message = format_response_for_slack(last_assistant_message)
        
        # For slack_button source, preserve the execution context
        if source == 'slack_button':
            suggestion_id = trigger_metadata.get('suggestion_id')
            
            # Get user info and suggestion details for proper attribution
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        # Get user email
                        cursor.execute(
                            "SELECT email FROM users WHERE id = %s",
                            (user_id,)
                        )
                        user_row = cursor.fetchone()
                        username = "user"
                        if user_row and user_row[0]:
                            user_email = user_row[0]
                            # Extract username from email (before @)
                            username = user_email.split('@')[0] if '@' in user_email else user_email
                        
                        # Get suggestion title and command
                        suggestion_title = None
                        suggestion_command = None
                        if suggestion_id:
                            cursor.execute(
                                "SELECT title, command FROM incident_suggestions WHERE id = %s",
                                (suggestion_id,)
                            )
                            sugg_row = cursor.fetchone()
                            if sugg_row:
                                suggestion_title = sugg_row[0]
                                suggestion_command = sugg_row[1]
                        
                        # Build attribution header with separator
                        header_lines = ["━━━━━━━━━━━━━━"]
                        if suggestion_title:
                            # Make title prominent with underline effect
                            header_lines.append(f"*{suggestion_title}*")
                            header_lines.append("─" * min(len(suggestion_title), 50))  # Underline
                        header_lines.append(f"_Executed by {username}_")
                        if suggestion_command:
                            cmd_display = suggestion_command[:100] + '...' if len(suggestion_command) > 100 else suggestion_command
                            header_lines.append(f"`{cmd_display}`")
                        header_lines.append("")  # Empty line before results
                        
                        attribution = "\n".join(header_lines)
                        formatted_message = f"{attribution}\n{formatted_message}"
                        
            except Exception as e:
                logger.warning(f"[BackgroundChat] Could not get execution context for attribution: {e}")
        
        # Get Slack client and send the response
        client = get_slack_client_for_user(user_id)
        if not client:
            logger.error(f"[BackgroundChat] Could not get Slack client for user {user_id}")
            return
        
        # Update the "Thinking..." message if we have the timestamp, otherwise send a new message
        if thinking_message_ts:
            client.update_message(
                channel=channel,
                ts=thinking_message_ts,
                text=formatted_message
            )
        else:
            # Fallback: send as new message if we don't have the thinking message timestamp
            client.send_message(
                channel=channel,
                text=formatted_message,
                thread_ts=thread_ts
            )
        
    except Exception as e:
        logger.error(f"[BackgroundChat] Error sending response to Slack: {e}", exc_info=True)
        raise


def create_background_chat_session(
    user_id: str,
    title: str,
    trigger_metadata: Optional[Dict[str, Any]] = None,
    incident_id: Optional[str] = None,
) -> str:
    """Create a new chat session for a background chat.
    
    This creates the session in the database before the background task runs,
    ensuring the chat appears in the user's chat history immediately.
    
    Args:
        user_id: The user ID
        title: Title for the chat session
        trigger_metadata: Optional metadata about the trigger source
        incident_id: Optional incident ID to link this chat session to
    
    Returns:
        The created session ID
    """
    session_id = str(uuid.uuid4())
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Set user context for RLS
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                
                # Create the session with initial metadata and in_progress status
                ui_state = {
                    "selectedMode": "ask",
                    "isBackground": True,
                }
                if trigger_metadata:
                    ui_state["triggerMetadata"] = trigger_metadata
                
                cursor.execute("""
                    INSERT INTO chat_sessions (id, user_id, title, messages, ui_state, created_at, updated_at, is_active, status, incident_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    session_id,
                    user_id,
                    title,
                    json.dumps([]),
                    json.dumps(ui_state),
                    datetime.now(),
                    datetime.now(),
                    True,
                    "in_progress",  # Background chats start as in_progress
                    incident_id,  # Link to incident if provided
                ))
            conn.commit()
            
            logger.info(f"[BackgroundChat] Created session {session_id} for user {user_id} (status=in_progress, incident_id={incident_id})")
            
    except Exception as e:
        logger.exception(f"[BackgroundChat] Failed to create session: {e}")
        raise
    
    return session_id

@celery_app.task(name="chat.background.cleanup_stale_sessions")
def cleanup_stale_background_chats() -> Dict[str, Any]:
    """Cleanup background chat sessions stuck in 'in_progress' for >20 minutes.
    
    Runs periodically to mark abandoned sessions as 'failed' and update their incidents.
    """    
    stale_threshold = datetime.now() - __import__('datetime').timedelta(minutes=20)
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Find stale sessions with their incident IDs
                cursor.execute("""
                    SELECT cs.id, cs.user_id, i.id as incident_id
                    FROM chat_sessions cs
                    LEFT JOIN incidents i ON i.aurora_chat_session_id = cs.id::uuid
                    WHERE cs.status = 'in_progress' AND cs.updated_at < %s
                """, (stale_threshold,))
                
                stale_sessions = cursor.fetchall()
                
                if not stale_sessions:
                    logger.info("[BackgroundChat:Cleanup] No stale sessions found")
                    return {"cleaned": 0}
                
                # Mark sessions as failed
                cursor.execute("""
                    UPDATE chat_sessions 
                    SET status = 'failed', updated_at = %s 
                    WHERE status = 'in_progress' AND updated_at < %s
                """, (datetime.now(), stale_threshold))
                
                cleaned_count = cursor.rowcount
            conn.commit()
            
            # Update associated incidents (both aurora_status and status)
            if stale_sessions:
                with conn.cursor() as cursor:
                    for session_id, user_id, incident_id in stale_sessions:
                        if incident_id:
                            cursor.execute(
                                "UPDATE incidents SET aurora_status = 'error', status = 'analyzed', updated_at = %s WHERE id = %s",
                                (datetime.now(), incident_id)
                            )
                conn.commit()
            
            logger.info(f"[BackgroundChat:Cleanup] Marked {cleaned_count} stale sessions as failed")
            return {"cleaned": cleaned_count, "session_ids": [s[0] for s in stale_sessions]}
            
    except Exception as e:
        logger.exception(f"[BackgroundChat:Cleanup] Failed: {e}")
        return {"error": str(e), "cleaned": 0}

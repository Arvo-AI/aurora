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
from utils.log_sanitizer import sanitize
from utils.notifications.email_service import get_email_service
from utils.auth.stateless_auth import get_user_email, get_credentials_from_db, set_rls_context
from utils.notifications.slack_notification_service import (
    send_slack_investigation_started_notification,
    send_slack_investigation_completed_notification,
)
from utils.notifications.google_chat_notification_service import (
    send_google_chat_investigation_started_notification,
    send_google_chat_investigation_completed_notification,
)
from connectors.google_chat_connector.client import get_chat_app_client
from utils.db.connection_pool import db_pool
from chat.background.visualization_generator import update_visualization
from chat.backend.constants import MAX_TOOL_OUTPUT_CHARS, INFRASTRUCTURE_TOOLS


logger = logging.getLogger(__name__)


def cancel_rca_for_incident(incident_id: str, user_id: str) -> bool:
    """Cancel a running RCA for an incident by revoking its Celery task.
    
    This is the proper way to stop an RCA when an incident is merged.
    It uses Celery's task revocation with SIGTERM to gracefully stop the task.
    
    Args:
        incident_id: The incident ID whose RCA should be cancelled
        user_id: User ID for RLS context
        
    Returns:
        True if a task was found and revoked, False otherwise
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:CancelRCA]")
                cursor.execute(
                    "SELECT rca_celery_task_id, aurora_status FROM incidents WHERE id = %s",
                    (incident_id,)
                )
                row = cursor.fetchone()
                
                if not row:
                    logger.info(f"[RCA-CANCEL] Incident {incident_id} not found")
                    return False
                
                task_id, aurora_status = row[0], row[1]
                
                if not task_id:
                    logger.info(f"[RCA-CANCEL] No Celery task ID found for incident {incident_id}")
                    return False
                
                # Only revoke if RCA is actually running
                if aurora_status != 'running':
                    logger.info(
                        f"[RCA-CANCEL] RCA for incident {incident_id} is not running (status={aurora_status}), skipping revocation"
                    )
                    return False
                
                # Revoke the task with SIGTERM for graceful shutdown
                celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM')
                logger.info(f"[RCA-CANCEL] Revoked Celery task {task_id} for incident {incident_id}")
                
                # Clear the task ID from the database
                cursor.execute(
                    "UPDATE incidents SET rca_celery_task_id = NULL WHERE id = %s",
                    (incident_id,)
                )
                conn.commit()
                
                return True
                
    except Exception as e:
        logger.error(f"[RCA-CANCEL] Failed to cancel RCA for incident {incident_id}: {e}")
        return False


def _extract_tool_calls_for_viz(
    session_id: str,
    user_id: str,
    llm_context: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict]:
    """Extract infrastructure tool calls for visualization.

    Accepts a pre-loaded ``llm_context`` to avoid an extra SELECT on the happy
    path (``_ensure_llm_context_history`` already fetches the column).
    """
    try:
        if llm_context is None:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:ExtractToolCalls]")
                    cursor.execute(
                        """
                        SELECT llm_context_history
                        FROM chat_sessions
                        WHERE id = %s AND user_id = %s
                        """,
                        (session_id, user_id),
                    )
                    row = cursor.fetchone()
            if not row or not row[0]:
                logger.warning(f"[Visualization] No llm_context_history for session {session_id}")
                return []
            llm_context = row[0]
            if isinstance(llm_context, str):
                llm_context = json.loads(llm_context)

        tool_calls = []
        for msg in llm_context:
            if isinstance(msg, dict) and msg.get('name') in INFRASTRUCTURE_TOOLS:
                tool_calls.append({
                    'tool': msg.get('name'),
                    'output': str(msg.get('content', ''))[:MAX_TOOL_OUTPUT_CHARS]
                })

        return tool_calls

    except Exception:
        logger.exception(f"[Visualization] Failed to extract tool calls for session {session_id}")
        return []


def _ensure_llm_context_history(
    session_id: str, user_id: str
) -> Optional[List[Dict[str, Any]]]:
    """Return the session's llm_context_history, rebuilding from UI messages if empty.

    Returns the deserialized context list so the caller can skip a second DB
    read. Returns ``None`` when the session row is missing or on error.
    """
    from langchain_core.messages import AIMessage, ToolMessage
    from chat.backend.agent.utils.llm_context_manager import LLMContextManager
    from chat.backend.agent.utils.persistence.context_manager import ContextManager

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:EnsureLLMContext]")
                cursor.execute(
                    """
                    SELECT llm_context_history, messages
                    FROM chat_sessions
                    WHERE id = %s AND user_id = %s
                    """,
                    (session_id, user_id),
                )
                row = cursor.fetchone()

        if not row:
            return None

        llm_context, ui_messages = row[0], row[1]
        if isinstance(llm_context, str):
            try:
                llm_context = json.loads(llm_context) if llm_context else []
            except (ValueError, TypeError) as e:
                logger.error(
                    f"[BackgroundChat] Malformed llm_context_history JSON for session {session_id}: {e}; treating as empty"
                )
                llm_context = []
        if llm_context:
            return llm_context

        if isinstance(ui_messages, str):
            try:
                ui_messages = json.loads(ui_messages) if ui_messages else []
            except (ValueError, TypeError) as e:
                logger.error(
                    f"[BackgroundChat] Malformed messages JSON for session {session_id}: {e}; cannot rebuild context"
                )
                return []
        if not ui_messages:
            return []

        logger.warning(
            f"[BackgroundChat] llm_context_history empty for session {session_id}; "
            f"rebuilding from UI messages as fallback"
        )

        rebuilt_messages: List[Any] = []
        for ui_msg in ui_messages:
            tool_calls_ui = ui_msg.get("toolCalls") or []
            if not tool_calls_ui:
                continue

            ai_tool_calls = []
            tool_messages: List[ToolMessage] = []
            for tc in tool_calls_ui:
                tool_call_id = tc.get("id")
                if not tool_call_id:
                    continue
                tool_name = tc.get("tool_name") or tc.get("name") or "unknown"
                try:
                    args = json.loads(tc["input"]) if isinstance(tc.get("input"), str) else (tc.get("input") or {})
                except (ValueError, TypeError):
                    args = {}
                ai_tool_calls.append({
                    "id": tool_call_id,
                    "name": tool_name,
                    "args": args,
                    "type": "tool_call",
                })
                output = tc.get("output")
                if output is None:
                    continue
                tool_messages.append(
                    ToolMessage(
                        content=str(output),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
                )

            if not ai_tool_calls:
                continue

            if ui_msg.get("sender") == "bot":
                ai_content = ui_msg.get("text") or ui_msg.get("content") or ""
            else:
                ai_content = ""
            rebuilt_messages.append(AIMessage(content=ai_content, tool_calls=ai_tool_calls))
            rebuilt_messages.extend(tool_messages)

        if not rebuilt_messages:
            return []

        # Bypass ContextManager.save_context_history's Redis dedup — a stale
        # hash from the lost async save would cause this forced rewrite to
        # no-op. We already know the DB column is empty, so write directly.
        saved = ContextManager._get_instance()._execute_actual_save(
            session_id, user_id, rebuilt_messages
        )
        if not saved:
            logger.error(
                f"[BackgroundChat] Forced rewrite of llm_context_history failed for session {session_id}"
            )
            return None
        logger.info(
            f"[BackgroundChat] Rebuilt llm_context_history for session {session_id} "
            f"with {len(rebuilt_messages)} synthetic messages"
        )
        return [LLMContextManager.serialize_message(m) for m in rebuilt_messages]

    except Exception:
        logger.exception(
            f"[BackgroundChat] Failed to ensure llm_context_history for session {session_id}"
        )
        return None


_RATE_LIMIT_WINDOW_SECONDS = 300  # 5 minute window
_RATE_LIMIT_MAX_REQUESTS = 5  # Max 5 background chats per window

# RCA sources that use rca_context in system prompt
_RCA_SOURCES = {'grafana', 'datadog', 'netdata', 'splunk', 'slack', 'google_chat', 'pagerduty', 'dynatrace', 'jenkins', 'cloudbees', 'spinnaker', 'newrelic', 'chat', 'opsgenie'}

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
    """Check which integrations are connected for a user.

    Delegates to SkillRegistry as the single source of truth for connection
    checks, avoiding drift between hardcoded checks here and SKILL.md
    connection_check definitions.
    """
    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        registry = SkillRegistry.get_instance()
        connected_ids = registry.get_connected_skill_ids(user_id)
        integrations = {skill_id: True for skill_id in connected_ids}
        logger.info("[BackgroundChat] Connected integrations via SkillRegistry: %s", list(integrations.keys()))
        return integrations
    except Exception as e:
        logger.warning("[BackgroundChat] SkillRegistry check failed, returning empty: %s", e)
        return {}


def _build_rca_context(
    user_id: str,
    trigger_metadata: Optional[Dict[str, Any]] = None,
    provider_preference: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build RCA context dict for background chats.

    This context is passed to State and used by prompt_builder to inject
    RCA instructions into the system prompt (not the user message).

    Single source of truth: cloud providers come from user_connections
    (role-based, always valid), integrations come from SkillRegistry
    (credential-validated). The agent only sees providers that actually work.

    Returns:
        Dict with source, providers, integrations, etc. or None if not an RCA source.
    """
    source = (trigger_metadata or {}).get('source', '').lower()
    if source not in _RCA_SOURCES:
        return None

    logger.info(f"[BackgroundChat] Building RCA context for source: {source}")

    # Get verified integrations from SkillRegistry (single source of truth)
    integrations = _get_connected_integrations(user_id)

    # Build verified providers list: cloud providers (role-based auth) +
    # SkillRegistry-validated integrations. Never show unverified providers.
    _cloud_providers = {'aws', 'gcp', 'azure', 'ovh', 'scaleway'}
    verified_cloud = []
    if not provider_preference:
        try:
            from utils.auth.stateless_auth import get_connected_providers
            all_db_providers = get_connected_providers(user_id)
            verified_cloud = [p for p in all_db_providers if p.lower() in _cloud_providers]
        except Exception as e:
            logger.warning(f"[BackgroundChat] Failed to fetch cloud providers: {e}")
    else:
        verified_cloud = [p for p in provider_preference if p.lower() in _cloud_providers]

    providers = sorted(set(verified_cloud + list(integrations.keys())))

    logger.info(f"[BackgroundChat] User {user_id} verified providers: {providers}, integrations: {list(integrations.keys())}")

    return {
        'source': source,
        'providers': providers,
        'integrations': integrations,
        'trigger_metadata': trigger_metadata,
        'user_id': user_id,
    }


@celery_app.task(
    bind=True, 
    name="chat.background.run_background_chat",
    time_limit=1800,  # Hard timeout: 30 minutes (task killed)
    soft_time_limit=1740  # Soft timeout: 29 minutes (exception raised, 60s grace for cleanup before hard kill)
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
    - Times out after 30 minutes to prevent hanging indefinitely

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
        # Link session and Celery task ID to incident if provided
        if incident_id:
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        if not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat]"):
                            logger.error("[BackgroundChat] Cannot resolve org_id for user %s, skipping incident linking", user_id)
                            raise ValueError(f"Missing org_id for user {user_id}")
                    # Ensure chat_sessions.incident_id is set (single source of truth)
                    # RLS already set on this conn at line above
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "UPDATE chat_sessions SET incident_id = %s WHERE id = %s AND incident_id IS NULL",
                            (incident_id, session_id)
                        )
                    conn.commit()

                    # Store session ID and Celery task ID (if not already set by webhook handler)
                    # RLS already set on this conn at line above
                    with conn.cursor() as cursor:
                        # First check if there's already a task ID set
                        cursor.execute(
                            "SELECT rca_celery_task_id FROM incidents WHERE id = %s",
                            (incident_id,)
                        )
                        row = cursor.fetchone()
                        existing_task_id = row[0] if row and row[0] else None
                        
                        if existing_task_id and existing_task_id != self.request.id:
                            logger.warning(
                                f"[BackgroundChat] Incident {incident_id} already has task ID {existing_task_id}, "
                                f"but this task is {self.request.id}. This may indicate a race condition or duplicate RCA start."
                            )
                        
                        cursor.execute(
                            """UPDATE incidents 
                               SET aurora_chat_session_id = %s, 
                                   rca_celery_task_id = COALESCE(rca_celery_task_id, %s)
                               WHERE id = %s""",
                            (session_id, self.request.id, incident_id)
                        )
                        conn.commit()
                        
                        if existing_task_id:
                            logger.info(
                                f"[BackgroundChat] Linked session {session_id} to incident {incident_id} "
                                f"(task ID already set to {existing_task_id})"
                            )
                        else:
                            logger.info(
                                f"[BackgroundChat] Linked session {session_id} and task {self.request.id} to incident {incident_id}"
                            )
                    
                    # Set incident aurora_status to running and stamp the moment
                    # the worker actually picked up the task. Used by the SRE
                    # metrics dashboard to compute pickup latency (MTTD as
                    # "time from webhook arrival to investigation start"). We
                    # COALESCE so a retry doesn't overwrite the original pickup.
                    pickup_at = datetime.now()
                    # RLS already set on this conn at line above
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """UPDATE incidents
                               SET aurora_status = %s,
                                   investigation_started_at = COALESCE(investigation_started_at, %s),
                                   updated_at = %s
                               WHERE id = %s""",
                            ("running", pickup_at, pickup_at, incident_id),
                        )
                        conn.commit()
                        logger.info(f"[BackgroundChat] Set incident {incident_id} aurora_status to 'running' at start of RCA")

                    # Record lifecycle event for RCA start
                    try:
                        # RLS already set on this conn at line above
                        with conn.cursor() as cursor:
                            cursor.execute(
                                """INSERT INTO incident_lifecycle_events
                                   (incident_id, user_id, org_id, event_type, new_value)
                                   VALUES (%s, %s, %s, %s, %s)""",
                                (incident_id, user_id, None, 'rca_started', 'running')
                            )
                            conn.commit()
                            logger.info(f"[BackgroundChat] Recorded lifecycle event 'rca_started' for incident {incident_id}")
                    except Exception as le:
                        logger.error(f"[BackgroundChat] Failed to record lifecycle event 'rca_started' for incident {incident_id}: {le}")
                    
                    # Send investigation started notifications (if enabled)
                    # Skip notifications if explicitly disabled (e.g., for Slack @mentions)
                    # For email: need both general notifications AND start notifications enabled
                    # For Slack: only need general notifications (no separate start preference since start message is overwritten by end message)
                    email_general_enabled = _is_rca_email_notification_enabled(user_id)
                    email_start_enabled = _is_rca_email_start_notification_enabled(user_id)
                    email_start_notification_enabled = email_general_enabled and email_start_enabled
                    
                    slack_notification_enabled = _has_slack_connected(user_id)
                    google_chat_notification_enabled = _has_google_chat_connected(user_id)
                    
                    if send_notifications and (email_start_notification_enabled or slack_notification_enabled or google_chat_notification_enabled):
                        _send_rca_notification(user_id, incident_id, 'started', 
                            email_enabled=email_start_notification_enabled,
                            slack_enabled=slack_notification_enabled,
                            google_chat_enabled=google_chat_notification_enabled
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
        _update_session_status(session_id, "completed", user_id=user_id)
        
        # Update incident status to analyzed if incident_id provided
        if incident_id:
            # Clear the Celery task ID since we're done
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:ClearTaskID]")
                        cursor.execute(
                            "UPDATE incidents SET rca_celery_task_id = NULL WHERE id = %s",
                            (incident_id,)
                        )
                        conn.commit()
            except Exception as e:
                logger.warning(f"[BackgroundChat] Failed to clear task ID for incident {incident_id}: {e}")
            
            _update_incident_status(incident_id, "analyzed", user_id=user_id)

            _update_incident_aurora_status(incident_id, "summarizing",  user_id=user_id)

            # Post RCA-complete comment to linked JSM incident
            if (trigger_metadata or {}).get("source") == "opsgenie":
                try:
                    from routes.opsgenie.opsgenie_routes import _build_client_from_creds, _get_stored_opsgenie_credentials
                    jsm_creds = _get_stored_opsgenie_credentials(user_id)
                    if jsm_creds and jsm_creds.get("auth_type") == "jsm_basic":
                        jsm_client = _build_client_from_creds(jsm_creds)
                        if jsm_client and hasattr(jsm_client, "find_incident_for_alert"):
                            alert_title = (trigger_metadata or {}).get("alert_title", "")
                            issue_key = jsm_client.find_incident_for_alert(alert_title)
                            if issue_key:
                                summary = result.get("summary", "RCA complete. See Aurora for details.")
                                comment = f"Aurora RCA complete.\n\n{summary[:500]}" if len(str(summary)) > 10 else "Aurora RCA analysis complete."
                                frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
                                if frontend_url and incident_id:
                                    comment += f"\n\nView in Aurora: {frontend_url}/incidents/{incident_id}"
                                jsm_client.add_comment_to_issue(issue_key, comment)
                                logger.info("[BackgroundChat] Posted RCA-complete comment to linked JSM incident")
                except Exception as e:
                    logger.debug(f"[BackgroundChat] Could not post JSM RCA-complete comment: {e}")

            # Determine severity from RCA if currently unknown
            try:
                _determine_severity_from_rca(incident_id, session_id, user_id)
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
                _update_incident_aurora_status(incident_id, "complete", user_id=user_id)
            
            # Generate final complete visualization
            try:
                tool_calls = result.get('tool_calls', [])
                logger.info(f"[BackgroundChat] Using {len(tool_calls)} tool calls from result for final visualization")
                
                update_visualization.apply_async(kwargs={
                    'incident_id': incident_id,
                    'user_id': user_id,
                    'session_id': session_id,
                    'force_full': True,
                    'tool_calls_json': json.dumps(tool_calls) if tool_calls else None
                })
                logger.info(f"[BackgroundChat] Queued final visualization for incident {incident_id}")
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to generate final visualization: {e}")
        
        # Send response back to Slack if this was triggered from Slack
        if trigger_metadata and trigger_metadata.get('source') in ['slack', 'slack_button']:
            try:
                _send_response_to_slack(user_id, session_id, trigger_metadata)
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to send response to Slack: {e}", exc_info=True)
        
        # Send response back to Google Chat if this was triggered from Google Chat
        if trigger_metadata and trigger_metadata.get('source') in ['google_chat', 'google_chat_button']:
            try:
                _send_response_to_google_chat(user_id, session_id, trigger_metadata)
            except Exception as e:
                logger.error(f"[BackgroundChat] Failed to send response to Google Chat: {e}", exc_info=True)
        
        completed_successfully = True
        logger.info(f"[BackgroundChat] Completed for session {session_id}")
        return result
    
    except SoftTimeLimitExceeded:
        logger.error(f"[BackgroundChat] Timeout after 30 minutes for session {session_id}")
        _update_session_status(session_id, "failed", user_id=user_id)
        if incident_id:
            _update_incident_aurora_status(incident_id, "error", user_id=user_id)
        return {
            "session_id": session_id,
            "status": "failed",
            "error": "Background chat exceeded 30 minute timeout",
        }

    except Exception as e:
        logger.exception(f"[BackgroundChat] Failed for session {session_id}: {e}")
        _update_session_status(session_id, "failed", user_id=user_id)
        if incident_id:
            _update_incident_aurora_status(incident_id, "error", user_id=user_id)
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
                        set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:FinallyCleanup]")
                        cursor.execute(
                            "UPDATE chat_sessions SET status = 'failed', updated_at = %s WHERE id = %s AND status = 'in_progress'",
                            (datetime.now(), session_id)
                        )
                        if cursor.rowcount > 0:
                            conn.commit()
                            logger.warning(f"[BackgroundChat] Finally block marked session {session_id} as failed")
                            _propagate_suggestion_status(session_id, "failed")
            except Exception as cleanup_err:
                logger.error(f"[BackgroundChat] Failed to cleanup session {session_id}: {cleanup_err}")


# ---------------------------------------------------------------------------
# Jira follow-up helpers
# ---------------------------------------------------------------------------

_JIRA_TOOL_NAMES = frozenset(('jira_add_comment', 'jira_create_issue'))


def _session_has_successful_jira_action(session_id: str, user_id: str) -> bool:
    """Return True if the session already contains a successful Jira tool call.

    Used after _run_jira_action to confirm the agent actually filed.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:JiraActionCheck]")
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s", (session_id,)
                )
                row = cursor.fetchone()
                if not row or not row[0]:
                    return False
                msgs = row[0] if isinstance(row[0], list) else json.loads(row[0])
                return _any_jira_success(msgs)
    except Exception as exc:
        logger.warning(f"[JiraFollowup] Failed to check existing actions: {exc}")
    return False


def _any_jira_success(msgs: list) -> bool:
    """Scan chat messages for a successful Jira tool call."""
    for msg in msgs:
        for tc in (msg.get('toolCalls') or []):
            if (tc.get('tool_name') or '').lower() not in _JIRA_TOOL_NAMES:
                continue
            if _tool_call_succeeded(tc):
                return True
    return False


def _tool_call_succeeded(tc: dict) -> bool:
    """Return True if a single tool-call dict indicates success."""
    output = tc.get('output') or ''
    try:
        parsed = json.loads(output) if isinstance(output, str) else output
        if isinstance(parsed, dict) and parsed.get('status') == 'success':
            return True
    except (json.JSONDecodeError, TypeError, ValueError):
        if '"success"' in str(output):
            return True
    return False


def _build_jira_followup_prompt(jira_mode: str, service_name: str = "") -> str:
    """Return the prompt for the Jira filing step after investigation completes."""
    comment_format = (
        "\n\nFormat the comment using markdown — it will be rendered as rich text in Jira.\n"
        "Use this structure:\n\n"
        "## Aurora RCA — {Short Title}\n\n"
        "### Root Cause\n"
        "{1-2 sentences. Be specific about the failure mechanism.}\n\n"
        "### Impact\n"
        "{1 sentence. Service, duration, user-facing effect.}\n\n"
        "### Evidence\n"
        "- {Key data point 1 with specific numbers/timestamps}\n"
        "- {Key data point 2}\n\n"
        "### Remediation\n"
        "1. **Immediate**: {What to do right now}\n"
        "2. **Follow-up**: {Prevent recurrence}\n\n"
        "RULES:\n"
        "- Keep it concise (15-25 lines max)\n"
        "- Use **bold** for emphasis on key terms\n"
        "- Use `code` for service names, commands, config values\n"
        "- Use bullet lists for evidence, numbered lists for remediation steps\n"
        "- No investigation logs, no 'I analyzed...', third person factual tone\n"
        "- Include specific numbers, timestamps, and metric values\n\n"
        "After the tool call succeeds, it returns a `url` field. "
        "Include this link in your response as a markdown link: [View in Jira](URL)"
    )

    issue_format = (
        "\n\nFormat the issue description using markdown — it will be rendered as rich text in Jira.\n"
        "Use this structure:\n\n"
        "## Summary\n"
        "{2-3 sentence overview of the incident.}\n\n"
        "## Root Cause\n"
        "{Detailed explanation of what went wrong and why.}\n\n"
        "## Impact\n"
        "- **Service**: {affected service(s)}\n"
        "- **Duration**: {how long}\n"
        "- **Severity**: {critical/high/medium/low}\n"
        "- **User-facing**: {yes/no and what users saw}\n\n"
        "## Evidence\n"
        "- {Key finding 1 with specific data}\n"
        "- {Key finding 2}\n"
        "- {Key finding 3}\n\n"
        "## Remediation\n"
        "### Immediate\n"
        "1. {Step 1}\n"
        "2. {Step 2}\n\n"
        "### Long-term\n"
        "1. {Preventive measure 1}\n"
        "2. {Preventive measure 2}\n\n"
        "RULES:\n"
        "- Use **bold** for key terms, `code` for service names/commands\n"
        "- Use bullet lists for evidence, numbered lists for action items\n"
        "- No investigation logs, third person factual tone\n"
        "- Include specific numbers, timestamps, metric values\n\n"
        "For the summary field, use: 'Incident: {service} — {short description}'\n\n"
        "After the tool call succeeds, it returns a `url` field. "
        "Include this link in your response as a markdown link: [View in Jira](URL)"
    )

    svc = service_name.replace("\\", "\\\\").replace('"', '\\"') if service_name else ""

    base = (
        "Your investigation is complete. Now file your findings in Jira.\n\n"
        "IMPORTANT: Use the project key from issues you already found earlier "
        "in this conversation. Do NOT guess project keys like 'OPS' or 'PROJECT' "
        "— use the real key you saw in search results.\n\n"
        "1. Search for an existing Jira issue related to this incident:\n"
    )
    if svc:
        base += (
            f"   jira_search_issues(jql='text ~ \"{svc}\" AND type in "
            "(Bug, Incident) ORDER BY updated DESC')\n"
        )
    else:
        base += (
            "   jira_search_issues(jql='text ~ \"<service_name>\" AND type in "
            "(Bug, Incident) ORDER BY updated DESC')\n"
        )
    if jira_mode == "comment_only":
        return (
            base
            + "2. Add your RCA findings as a single comment on the most relevant issue "
            "using jira_add_comment.\n\n"
            "You are in COMMENT ONLY mode. Do NOT create new issues.\n"
            "File EXACTLY ONE comment. Never more.\n\n"
            "Execute the tool calls now — do not just describe what you would do."
            + comment_format
        )
    return (
        base
        + "2. If a matching issue is found:\n"
        "   - Add your RCA findings as a single comment using jira_add_comment.\n"
        "   - Do NOT create a new issue.\n"
        "3. If NO matching issue is found:\n"
        "   - Create one using jira_create_issue with:\n"
        "     project_key from the search results, "
        f"summary like 'Incident: {svc or '<service_name>'} — brief description', issue_type='Bug'.\n"
        "   - Put the full RCA findings in the description field.\n"
        "   - Do NOT add a separate comment — the description is enough.\n\n"
        "File EXACTLY ONE Jira action (one comment OR one issue). Never both.\n\n"
        "Execute the tool calls now — do not just describe what you would do."
        + issue_format
    )


def _snapshot_session_messages(session_id: str, user_id: str) -> list:
    """Return the current UI messages list for the session."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:SnapshotMessages]")
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s", (session_id,)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return row[0] if isinstance(row[0], list) else json.loads(row[0])
    except Exception as exc:
        logger.error(f"[JiraFollowup] Failed to snapshot messages: {exc}")
    return []


def _merge_investigation_messages(session_id: str, investigation_messages: list, followup_prompt_prefix: str = "", user_id: str = "") -> None:
    """Replace session messages with investigation + Jira-only follow-up messages.
    
    The Jira follow-up workflow saves compressed investigation context + Jira
    messages to the session. We need the full investigation messages followed by
    only the new Jira-specific messages (to avoid duplicating the compressed
    context that the workflow injected).
    """
    if not investigation_messages:
        return
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:MergeMessages]")
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s", (session_id,)
                )
                row = cursor.fetchone()
                followup = (row[0] if isinstance(row[0], list) else json.loads(row[0])) \
                    if row and row[0] else []

                # Find where the Jira-specific messages start by looking for the
                # follow-up prompt. Everything before it is re-injected context
                # that duplicates the investigation.
                jira_start_idx = 0
                if followup_prompt_prefix:
                    for i, msg in enumerate(followup):
                        text = msg.get('text') or msg.get('content') or ''
                        if msg.get('sender') == 'user' and text.startswith(followup_prompt_prefix[:80]):
                            jira_start_idx = i
                            break

                jira_only = followup[jira_start_idx:] if jira_start_idx > 0 else followup
                merged = investigation_messages + jira_only
                cursor.execute(
                    "UPDATE chat_sessions SET messages = %s::jsonb WHERE id = %s",
                    (json.dumps(merged), session_id),
                )
                conn.commit()
        logger.info(
            f"[JiraFollowup] Merged messages: {len(investigation_messages)} investigation "
            f"+ {len(jira_only)} jira-only (skipped {len(followup) - len(jira_only)} context duplicates) "
            f"= {len(merged)} total"
        )
    except Exception as exc:
        logger.error(f"[JiraFollowup] Failed to merge messages: {exc}")


async def _run_jira_action(
    *,
    session_id: str,
    user_id: str,
    incident_id: Optional[str],
    provider_preference: Optional[List[str]],
    rca_context: dict,
    mode: str,
    wf,
    background_ws,
) -> None:
    """Run the Jira filing step after the RCA investigation completes.

    This is a deterministic second phase: investigate first, then file.
    """
    from chat.backend.agent.utils.state import State
    from chat.backend.agent.llm import ModelConfig
    from main_chatbot import process_workflow_async

    jira_mode = rca_context.get('integrations', {}).get('jira_mode', 'comment_only')

    service_name = ""
    if incident_id:
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[JiraAction]")
                    cur.execute("SELECT alert_service FROM incidents WHERE id = %s", (incident_id,))
                    row = cur.fetchone()
                    if row and row[0]:
                        service_name = row[0]
        except Exception as exc:
            logger.debug("[JiraAction] Could not look up service name: %s", exc)

    followup_text = _build_jira_followup_prompt(jira_mode, service_name=service_name)

    investigation_messages = _snapshot_session_messages(session_id, user_id=user_id)
    logger.info(f"[JiraAction] Saved {len(investigation_messages)} investigation messages")

    # Flush any pending async context save so the Jira follow-up can load
    # the full investigation history from the DB.
    try:
        from chat.backend.agent.utils.persistence.context_manager import ContextManager
        await ContextManager.flush_session(session_id)
    except Exception as exc:
        logger.warning(f"[JiraAction] Failed to flush context for {session_id}: {exc}")

    followup_state = State(
        user_id=user_id,
        session_id=session_id,
        incident_id=incident_id,
        provider_preference=provider_preference,
        selected_project_id=None,
        messages=[HumanMessage(content=followup_text)],
        question=followup_text,
        model=ModelConfig.RCA_MODEL,
        mode=mode,
        is_background=True,
        rca_context=rca_context,
    )
    logger.info(f"[JiraAction] Starting Jira step for {session_id} (jira_mode={jira_mode})")

    try:
        await process_workflow_async(wf, followup_state, background_ws, user_id, incident_id=incident_id)
        if hasattr(wf, '_wait_for_ongoing_tool_calls'):
            await wf._wait_for_ongoing_tool_calls()
    except Exception as exc:
        logger.error(f"[JiraAction] Failed: {exc}")
        _merge_investigation_messages(session_id, investigation_messages,
                                       followup_prompt_prefix=followup_text[:80], user_id=user_id)
        return

    _merge_investigation_messages(session_id, investigation_messages,
                                   followup_prompt_prefix=followup_text[:80], user_id=user_id)
    logger.info(f"[JiraAction] Completed for {session_id}")


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
            workflow=wf,  # Pass workflow so RCA context updates can be injected
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

        # --- Phase 2: Jira action ---
        # Investigation is done. Now deterministically file in Jira.
        if rca_context and rca_context.get('integrations', {}).get('jira') \
                and not _session_has_successful_jira_action(session_id, user_id=user_id):
            await _run_jira_action(
                session_id=session_id,
                user_id=user_id,
                incident_id=incident_id,
                provider_preference=provider_preference,
                rca_context=rca_context,
                mode=mode,
                wf=wf,
                background_ws=background_ws,
            )
        
        if incident_id:
            # Check if status was already set to complete (shouldn't happen, but log if it does)
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:StatusCheck]")
                    cursor.execute("SELECT aurora_status FROM incidents WHERE id = %s", (incident_id,))
                    row = cursor.fetchone()
                    if row and row[0] == 'complete':
                        logger.error(f"[BackgroundChat] ⚠️ WARNING: Incident {incident_id} aurora_status is already 'complete' before we set it! This indicates a race condition.")
        
        logger.info(f"[BackgroundChat] Workflow execution completed - all streams and tool calls finished")

        # Fallback: rebuild llm_context_history from UI messages if the save was lost.
        llm_context = _ensure_llm_context_history(session_id, user_id)
        tool_calls = _extract_tool_calls_for_viz(session_id, user_id, llm_context)
        logger.info(f"[BackgroundChat] Extracted {len(tool_calls)} tool calls for visualization")
        
        return {
            "session_id": session_id,
            "status": "completed",
            "trigger_metadata": trigger_metadata,
            "tool_calls": tool_calls,
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


def _update_session_status(session_id: str, status: str, user_id: str) -> None:
    """Update the status of a chat session.
    
    Args:
        session_id: The chat session ID
        status: New status ('in_progress', 'completed', 'failed', 'active')
        user_id: User ID for RLS context (required from Celery workers)
    """
    rows_updated = 0
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat]"):
                    return
                cursor.execute(
                    "UPDATE chat_sessions SET status = %s, updated_at = %s WHERE id = %s",
                    (status, datetime.now(), session_id)
                )
                rows_updated = cursor.rowcount
            conn.commit()
            logger.info(f"[BackgroundChat] Updated session {session_id} status to '{status}' (rows={rows_updated})")
    except Exception as e:
        logger.error(f"[BackgroundChat] Failed to update session {session_id} status to '{status}': {e}")
        return

    if rows_updated > 0 and status in ("completed", "failed"):
        _propagate_suggestion_status(session_id, status)


def _propagate_suggestion_status(session_id: str, status: str) -> None:
    """Propagate a terminal session status to any linked incident_suggestions rows.

    This ensures suggestions whose execution was kicked off via *session_id*
    get their ``execution_status`` moved out of ``in_progress`` (or ``executed``)
    when the session finishes or is cleaned up.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — incident_suggestions not RLS-protected
                cursor.execute(
                    """UPDATE incident_suggestions
                       SET execution_status = %s
                       WHERE execution_session_id = %s::uuid
                         AND execution_status IN ('in_progress', 'executed')""",
                    (status, session_id),
                )
                if cursor.rowcount > 0:
                    conn.commit()
                    logger.info(f"[BackgroundChat] Updated suggestion execution_status to '{status}' for session {session_id}")
    except Exception as e:
        logger.warning(f"[BackgroundChat] Failed to update suggestion execution_status for session {session_id}: {e}")


def _update_incident_aurora_status(incident_id: str, aurora_status: str, user_id: str) -> None:
    """Update incident aurora_status (running/complete/error).

    Args:
        incident_id: The incident ID
        aurora_status: New status ('running', 'complete', 'error', 'summarizing')
        user_id: The user ID for RLS context and lifecycle event recording
    """
    # Map aurora_status values to lifecycle event types
    _STATUS_EVENT_MAP = {
        'running': 'rca_started',
        'complete': 'rca_completed',
        'error': 'rca_error',
    }
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                resolved_org_id = set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:AuroraStatus]")
                if not resolved_org_id:
                    return
                now = datetime.now()
                if aurora_status == 'complete':
                    cursor.execute(
                        """UPDATE incidents
                           SET aurora_status = %s, updated_at = %s,
                               analyzed_at = COALESCE(analyzed_at, %s)
                           WHERE id = %s""",
                        (aurora_status, now, now, incident_id)
                    )
                else:
                    cursor.execute(
                        "UPDATE incidents SET aurora_status = %s, updated_at = %s WHERE id = %s",
                        (aurora_status, now, incident_id)
                    )
            conn.commit()
            logger.info(f"[BackgroundChat] Set incident {incident_id} aurora_status to '{aurora_status}'")

            # Record lifecycle event for trackable status transitions
            event_type = _STATUS_EVENT_MAP.get(aurora_status)
            if event_type and user_id:
                try:
                    # RLS already set on this conn above
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO incident_lifecycle_events
                               (incident_id, user_id, org_id, event_type, new_value)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (incident_id, user_id, resolved_org_id, event_type, aurora_status)
                        )
                    conn.commit()
                    logger.info(f"[BackgroundChat] Recorded lifecycle event '{event_type}' for incident {incident_id}")
                except Exception as le:
                    logger.error(f"[BackgroundChat] Failed to record lifecycle event '{event_type}' for incident {incident_id}: {le}")
    except Exception as e:
        logger.error(f"[BackgroundChat] Failed to update aurora_status: {e}")


def _determine_severity_from_rca(incident_id: str, session_id: str, user_id: str) -> None:
    """Determine severity from RCA chat if currently unknown."""
    try:
        from chat.backend.agent.llm import LLMManager
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
    except ImportError as ie:
        logger.error(f"[BackgroundChat] Failed to import LLMManager: {ie}")
        return
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:Severity]"):
                    return
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
                    from chat.backend.agent.llm import ModelConfig
                    severity_model = ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL
                    llm_manager = LLMManager()
                    model = llm_manager._get_or_create_model(severity_model)
                    
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
                        response = tracked_invoke(
                            model,
                            [HumanMessage(content=prompt)],
                            user_id=user_id,
                            session_id=session_id,
                            model_name=severity_model,
                            request_type="severity_determination",
                        )
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


def _update_incident_status(incident_id: str, status: str, user_id: str) -> None:
    """Update incident status when RCA completes.
    
    Args:
        incident_id: The incident ID
        status: New status ('investigating', 'analyzed')
        user_id: The user ID (required for RLS)
    
    Note: Will NOT update if current status is 'merged' to preserve merge state.
    """
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:IncidentStatus]"):
                    return
                cursor.execute(
                    """
                    UPDATE incidents 
                    SET status = %s, 
                        analyzed_at = CASE WHEN %s = 'analyzed' THEN %s ELSE analyzed_at END,
                        updated_at = %s
                    WHERE id = %s AND status != 'merged'
                    """,
                    (status, status, datetime.now(), datetime.now(), incident_id)
                )
                rows_updated = cursor.rowcount
            conn.commit()
            if rows_updated > 0:
                logger.info(f"[BackgroundChat] Updated incident {incident_id} status to '{status}' (rows={rows_updated})")
            else:
                logger.info(f"[BackgroundChat] Skipped status update for incident {incident_id} (likely merged)")
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
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:RCAEmailNotifCheck]")
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


def _has_google_chat_connected(user_id: str) -> bool:
    """Check if user's org has Google Chat connected with a service account."""
    try:
        config = get_credentials_from_db(user_id, "google_chat")
        if not config or not config.get("incidents_space_name"):
            return False
        return get_chat_app_client() is not None
    except Exception as e:
        logger.error(f"[GChatNotification] Error checking Google Chat connection: {e}")
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
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:RCAEmailStartNotifCheck]")
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


def _get_incident_data(incident_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch incident data from database.
    
    Args:
        incident_id: The incident UUID
        user_id: User ID for RLS context
        
    Returns:
        Dictionary with incident data or None if not found
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:GetIncidentData]")
                cursor.execute(
                    """
                    SELECT id, user_id, source_type, status, severity, alert_title, 
                           alert_service, aurora_status, aurora_summary, started_at, 
                           analyzed_at, created_at, slack_message_ts, google_chat_message_name
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
                        'google_chat_message_name': result[13] if len(result) > 13 else None,
                    }
        
        return None
        
    except Exception as e:
        logger.error(f"[EmailNotification] Error fetching incident data: {e}")
        return None


def _send_rca_notification(user_id: str, incident_id: str, event_type: str, email_enabled: bool = False, slack_enabled: bool = False, google_chat_enabled: bool = False, session_id: Optional[str] = None) -> None:
    """Send RCA email, Slack, and Google Chat notifications.
    
    Args:
        user_id: The user ID
        incident_id: The incident UUID
        event_type: 'started' or 'completed'
        email_enabled: Whether to send email notifications
        slack_enabled: Whether to send Slack notifications
        google_chat_enabled: Whether to send Google Chat notifications
        session_id: Optional chat session ID (used to extract last message for 'completed' notifications)
    """
    # Get incident data (needed for both email and Slack)
    incident_data = _get_incident_data(incident_id, user_id=user_id)
    if not incident_data:
        logger.error(f"[RCANotification] Incident {incident_id} not found")
        return
    
    # For completed notifications, extract the summary section from last message if not already present
    if event_type == 'completed' and session_id and not incident_data.get('aurora_summary'):
        try:
            from routes.slack.slack_events_helpers import extract_summary_section
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:RCANotifSummary]")
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
                            set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:RCANotifEmails]")
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
    
    # --- GOOGLE CHAT NOTIFICATIONS ---
    if google_chat_enabled:
        try:
            if event_type == 'started':
                send_google_chat_investigation_started_notification(user_id, incident_data)
            elif event_type == 'completed':
                send_google_chat_investigation_completed_notification(user_id, incident_data)
        except Exception as e:
            logger.error(f"[GChatNotification] Failed to send {event_type} notification: {e}", exc_info=True)


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
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:SlackResponse]")
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
                        # No RLS needed — users + incident_suggestions not RLS-protected
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


def _send_response_to_google_chat(user_id: str, session_id: str, trigger_metadata: Dict[str, Any]) -> None:
    """Send Aurora's response back to Google Chat after background chat completes."""
    try:
        from routes.google_chat.google_chat_events_helpers import format_response_for_google_chat

        space_name = trigger_metadata.get('space_name')
        thread_key = trigger_metadata.get('thread_key')
        thinking_message_name = trigger_metadata.get('thinking_message_name')
        source = trigger_metadata.get('source')

        if not space_name:
            logger.warning(f"[BackgroundChat] No Google Chat space in trigger_metadata for session {session_id}")
            return

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:GoogleChatResponse]")
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
                    messages = json.loads(messages)

                last_assistant_message = None
                for msg in reversed(messages):
                    if msg.get('sender') in ('bot', 'assistant'):
                        last_assistant_message = msg.get('text') or msg.get('content')
                        break

                if not last_assistant_message:
                    logger.warning(f"[BackgroundChat] No assistant message found in session {session_id}")
                    return

        formatted_message = format_response_for_google_chat(last_assistant_message)

        if source == 'google_chat_button':
            clicker_name = trigger_metadata.get('clicker_name')
            suggestion_title = trigger_metadata.get('suggestion_title')
            suggestion_command = trigger_metadata.get('suggestion_command')

            header_lines = ["━━━━━━━━━━━━━━"]
            if suggestion_title:
                header_lines.append(f"*{suggestion_title}*")
                header_lines.append("─" * min(len(suggestion_title), 50))
            if clicker_name:
                header_lines.append(f"_Executed by {clicker_name}_")
            if suggestion_command:
                cmd_display = suggestion_command[:100] + '...' if len(suggestion_command) > 100 else suggestion_command
                header_lines.append(f"`{cmd_display}`")
            header_lines.append("")

            attribution = "\n".join(header_lines)
            formatted_message = f"{attribution}\n{formatted_message}"

        client = get_chat_app_client()
        if not client:
            logger.error(f"[BackgroundChat] Could not get Google Chat client for user {user_id}")
            return

        if thinking_message_name:
            client.update_message(
                message_name=thinking_message_name,
                text=formatted_message
            )
        else:
            client.send_message(
                space_name=space_name,
                text=formatted_message,
                thread_key=thread_key
            )

    except Exception as e:
        logger.error(f"[BackgroundChat] Error sending response to Google Chat: {e}", exc_info=True)
        raise


def create_background_chat_session(
    user_id: str,
    title: str,
    trigger_metadata: Optional[Dict[str, Any]] = None,
    incident_id: Optional[str] = None,
    question: Optional[str] = None,
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
                # No RLS needed — users not RLS-protected
                cursor.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                org_id = row[0] if row and row[0] else None

                if not org_id:
                    logger.warning("No org_id found for user %s in background task session creation", user_id)
                    raise ValueError(f"Missing org_id for user {user_id}")
                
                if not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:session]"):
                    raise ValueError(f"Failed to set RLS context for user {user_id}")
                
                # Create the session with initial metadata and in_progress status
                ui_state = {
                    "selectedMode": "ask",
                    "isBackground": True,
                }
                if trigger_metadata:
                    ui_state["triggerMetadata"] = trigger_metadata
                
                initial_messages = []
                if question:
                    initial_messages.append({"sender": "user", "text": question})

                cursor.execute("""
                    INSERT INTO chat_sessions (id, user_id, org_id, title, messages, ui_state, created_at, updated_at, is_active, status, incident_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    session_id,
                    user_id,
                    org_id,
                    title,
                    json.dumps(initial_messages),
                    json.dumps(ui_state),
                    datetime.now(),
                    datetime.now(),
                    True,
                    "in_progress",
                    incident_id,
                ))
            conn.commit()
            
            logger.info(f"[BackgroundChat] Created session {sanitize(session_id)} for user {sanitize(user_id)} org {sanitize(org_id)} (status=in_progress, incident_id={sanitize(incident_id)})")
            
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
                # users table is not RLS-protected; iterate per-user to set RLS before querying protected tables
                cursor.execute("SELECT DISTINCT id, org_id FROM users WHERE org_id IS NOT NULL")
                all_users = cursor.fetchall()

                stale_sessions = []
                for uid, org_id in all_users:
                    cursor.execute("SET myapp.current_user_id = %s;", (uid,))
                    cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
                    conn.commit()
                    cursor.execute("""
                        SELECT cs.id, cs.user_id, i.id as incident_id
                        FROM chat_sessions cs
                        LEFT JOIN incidents i ON i.aurora_chat_session_id = cs.id::uuid
                        WHERE cs.status = 'in_progress' AND cs.updated_at < %s
                          AND cs.user_id = %s
                    """, (stale_threshold, uid))
                    stale_sessions.extend(cursor.fetchall())
                
                if not stale_sessions:
                    logger.info("[BackgroundChat:Cleanup] No stale sessions found")
                    return {"cleaned": 0}
                
                # Mark sessions as failed per-user (respecting RLS)
                actually_failed_ids = set()
                for session_id, user_id, incident_id in stale_sessions:
                    if not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:Cleanup]"):
                        continue
                    cursor.execute("""
                        UPDATE chat_sessions 
                        SET status = 'failed', updated_at = %s 
                        WHERE id = %s AND status = 'in_progress'
                        RETURNING id
                    """, (datetime.now(), session_id))
                    row = cursor.fetchone()
                    if row:
                        actually_failed_ids.add(str(row[0]))
                    conn.commit()
                
                cleaned_count = len(actually_failed_ids)
            
            # Propagate failed status only for sessions that were actually updated
            for session_id, user_id, incident_id in stale_sessions:
                if str(session_id) in actually_failed_ids:
                    _propagate_suggestion_status(str(session_id), 'failed')

            # Update associated incidents (both aurora_status and status)
            if actually_failed_ids:
                with conn.cursor() as cursor:
                    for session_id, user_id, incident_id in stale_sessions:
                        if incident_id and str(session_id) in actually_failed_ids:
                            if not user_id or not set_rls_context(cursor, conn, user_id, log_prefix="[BackgroundChat:Cleanup]"):
                                continue
                            cursor.execute(
                                "UPDATE incidents SET aurora_status = 'error', status = 'analyzed', updated_at = %s WHERE id = %s",
                                (datetime.now(), incident_id)
                            )
                            # Record lifecycle event for stale session error. Wrap in savepoint
                            # so a failure here doesn't abort the outer transaction and stop
                            # the status UPDATE from committing for remaining sessions.
                            try:
                                cursor.execute("SAVEPOINT sp_rca_error")
                                cursor.execute(
                                    """INSERT INTO incident_lifecycle_events
                                       (incident_id, user_id, org_id, event_type, new_value)
                                       VALUES (%s, %s, %s, %s, %s)""",
                                    (incident_id, user_id, None, 'rca_error', 'error')
                                )
                                cursor.execute("RELEASE SAVEPOINT sp_rca_error")
                            except Exception as lc_exc:
                                try:
                                    cursor.execute("ROLLBACK TO SAVEPOINT sp_rca_error")
                                except Exception as rb_exc:
                                    logger.debug(
                                        "[BackgroundChat:Cleanup] Rollback to sp_rca_error failed for incident %s: %s",
                                        incident_id, rb_exc,
                                    )
                                logger.warning(
                                    "[BackgroundChat:Cleanup] Failed to record rca_error lifecycle event "
                                    "for incident %s (user %s): %s",
                                    incident_id, user_id, lc_exc,
                                )
                conn.commit()
            
            logger.info(f"[BackgroundChat:Cleanup] Marked {cleaned_count} stale sessions as failed")
            return {"cleaned": cleaned_count, "session_ids": [s[0] for s in stale_sessions]}
            
    except Exception as e:
        logger.exception(f"[BackgroundChat:Cleanup] Failed: {e}")
        return {"error": str(e), "cleaned": 0}

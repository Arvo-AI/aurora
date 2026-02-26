from typing import Any, Dict, Optional, Annotated, List, Union, Callable, Tuple, Literal, Awaitable
import threading
import json
import asyncio
import logging
from functools import wraps
from datetime import datetime
import builtins
from pydantic import BaseModel, SkipValidation, Field
import contextvars
import subprocess
import tempfile
import os
import time
import shutil
from contextlib import asynccontextmanager
from threading import local
import concurrent.futures

from langchain_core.tools import StructuredTool
from .output_sanitizer import truncate_json_fields

# Import cloud tools
from .iac_tool import run_iac_tool

# Alias unified IaC entry point for export convenience
iac_tool = run_iac_tool
from .github_commit_tool import github_commit, GitHubCommitArgs
from .github_rca_tool import github_rca, GitHubRCAArgs
from .github_fix_tool import github_fix, GitHubFixArgs

# Visualization trigger caching
from cachetools import TTLCache
_viz_triggers: TTLCache = TTLCache(maxsize=100, ttl=3600)  # 1 hour TTL
from chat.backend.constants import MAX_TOOL_OUTPUT_CHARS
from .github_apply_fix_tool import github_apply_fix, GitHubApplyFixArgs
from .cloud_exec_tool import cloud_exec

from .zip_file_tool import analyze_zip_file
from .cloud_provider_utils import determine_target_provider_from_context
from .rag_indexer_tool import rag_index_zip, RAGIndexZipArgs
from .web_search_tool import web_search, WebSearchArgs
from .terminal_exec_tool import terminal_exec
from .tailscale_ssh_tool import tailscale_ssh
from .confluence_runbook_tool import confluence_runbook_parse, ConfluenceRunbookArgs
from .confluence_search_tool import (
    confluence_search_similar,
    confluence_search_runbooks,
    confluence_fetch_page,
    ConfluenceSearchSimilarArgs,
    ConfluenceSearchRunbookArgs,
    ConfluenceFetchPageArgs,
)
from .splunk_tool import (
    search_splunk,
    list_splunk_indexes,
    list_splunk_sourcetypes,
    is_splunk_connected,
    SplunkSearchArgs,
    SplunkListIndexesArgs,
    SplunkListSourcetypesArgs,
)
from .coroot_tool import (
    coroot_get_incidents,
    coroot_get_incident_detail,
    coroot_get_applications,
    coroot_get_app_detail,
    coroot_get_app_logs,
    coroot_get_traces,
    coroot_get_service_map,
    coroot_query_metrics,
    coroot_get_deployments,
    coroot_get_nodes,
    coroot_get_overview_logs,
    coroot_get_node_detail,
    coroot_get_costs,
    coroot_get_risks,
    is_coroot_connected,
    CorootGetIncidentsArgs,
    CorootGetIncidentDetailArgs,
    CorootGetApplicationsArgs,
    CorootGetAppDetailArgs,
    CorootGetAppLogsArgs,
    CorootGetTracesArgs,
    CorootGetServiceMapArgs,
    CorootQueryMetricsArgs,
    CorootGetDeploymentsArgs,
    CorootGetNodesArgs,
    CorootGetOverviewLogsArgs,
    CorootGetNodeDetailArgs,
    CorootGetCostsArgs,
    CorootGetRisksArgs,
)
from .dynatrace_tool import (
    query_dynatrace,
    is_dynatrace_connected,
    QueryDynatraceArgs,
)

# Import all context management functions from utils
from utils.cloud.cloud_utils import (
    get_user_context, set_user_context, get_state_context, get_workflow_context,
    set_websocket_context, get_websocket_context, get_selected_project_id, 
    set_selected_project_id, set_tool_capture, get_tool_capture,
    get_provider_preference, set_provider_preference, _set_ctx, get_mode_from_context
)
from chat.backend.agent.access import ModeAccessController

# Thread-local storage for user context and WebSocket sender
_context = threading.local()

# Global lock for WebSocket sending to prevent message corruption
_websocket_send_lock = threading.Lock()

# Lock for WebSocket connection management
_websocket_connection_lock = threading.Lock()
_websocket_connections: Dict[Tuple[str, str], Tuple[Any, Any, int]] = {}

# -----------------------------------------------------------------------------
# WebSocket connection management functions (keep these in cloud_tools.py)
# -----------------------------------------------------------------------------

def register_websocket_connection(user_id: str, session_id: str, websocket_sender, event_loop, connection_id: int):
    """Register a new WebSocket connection for a user/session pair."""
    with _websocket_connection_lock:
        key = (user_id, session_id)
        _websocket_connections[key] = (websocket_sender, event_loop, connection_id)
        logging.info(f"WEBSOCKET: Registered connection {connection_id} for user {user_id}, session {session_id}")

def unregister_websocket_connection(user_id: str, session_id: str):
    """Unregister a WebSocket connection when it's closed."""
    with _websocket_connection_lock:
        key = (user_id, session_id)
        if key in _websocket_connections:
            old_connection_id = _websocket_connections[key][2]
            del _websocket_connections[key]
            logging.info(f"WEBSOCKET: Unregistered connection {old_connection_id} for user {user_id}, session {session_id}")

def get_active_websocket_connection(user_id: str, session_id: str):
    """Get the currently active WebSocket connection for a user/session pair."""
    with _websocket_connection_lock:
        key = (user_id, session_id)
        return _websocket_connections.get(key)

def update_workflow_websocket_context(user_id: str, session_id: str):
    """Update the workflow's WebSocket context with the current active connection."""
    active_connection = get_active_websocket_connection(user_id, session_id)
    if active_connection:
        websocket_sender, event_loop, connection_id = active_connection
        set_websocket_context(websocket_sender, event_loop)
        logging.debug(f"WEBSOCKET: Updated workflow context with connection {connection_id} for user {user_id}, session {session_id}")
        
        # Also update the Agent's websocket_sender if we can find the workflow
        try:
            workflow = get_workflow_context()
            if workflow and hasattr(workflow, 'agent') and hasattr(workflow.agent, 'update_websocket_sender'):
                workflow.agent.update_websocket_sender(websocket_sender, event_loop)
                logging.debug(f"WEBSOCKET: Updated Agent websocket_sender for user {user_id}, session {session_id}")
            else:
                logging.warning(f"WEBSOCKET: Could not find workflow or agent to update websocket_sender")
        except Exception as e:
            logging.warning(f"WEBSOCKET: Error updating Agent websocket_sender: {e}")
        
        return True
    else:
        logging.warning(f"WEBSOCKET: No active connection found for user {user_id}, session {session_id}")
        return False

def validate_websocket_message(data):
    """Validate that data can be safely serialized as JSON for WebSocket transmission."""
    try:
        # Try to serialize and deserialize to ensure it's valid
        json_str = json.dumps(data, ensure_ascii=False)
        parsed_back = json.loads(json_str)
        return True, json_str
    except Exception as e:
        logging.error(f"WebSocket message validation failed: {e}")
        return False, None

def _send_ws_message_now(websocket_sender, message_data: Dict[str, Any], tool_name: str, fallback_message: Optional[str] = None, connection_id: str = "unknown") -> None:
    """Send a validated WebSocket message immediately in the current thread.

    Extracted from the nested function inside send_websocket_message for clarity.
    """
    with _websocket_send_lock:
        try:

            # Validate the message before sending
            is_valid, json_message = validate_websocket_message(message_data)

            if not is_valid and fallback_message:
                logging.error(f"Failed to validate WebSocket message for {tool_name}, sending fallback")
                # Create fallback message
                fallback_data = {
                    "type": message_data.get("type", "tool_result"),
                    "data": {
                        "tool_name": str(tool_name),
                        "output": fallback_message,
                        "status": message_data.get("data", {}).get("status", "completed"),
                        "timestamp": str(datetime.now().isoformat())
                    }
                }
                is_valid, json_message = validate_websocket_message(fallback_data)

            if not is_valid:
                logging.error(f"WebSocket message validation failed for {tool_name}")
                return

            # Create a new event loop in this thread if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Run the websocket send coroutine
            logging.debug(f"📤 Sending WebSocket message for {tool_name} via connection {connection_id}: {json_message[:200]}...")
            if websocket_sender:  # Type checker hint
                loop.run_until_complete(websocket_sender(json_message))
                logging.info(f"✅ Sent WebSocket message for {tool_name} via connection {connection_id}")

        except Exception as e:
            if "ConnectionClosed" in str(e) or "no close frame" in str(e):
                logging.warning(f"WebSocket connection {connection_id} closed during message send for {tool_name}")
            elif "AssertionError" in str(e) or "permessage_deflate" in str(e):
                logging.error(f"WebSocket compression error for tool {tool_name} via connection {connection_id}: {e}")
            else:
                logging.error(f"Failed to send WebSocket message for {tool_name} via connection {connection_id}: {e}")
                logging.debug(f"Error details: {str(e)}")

def send_websocket_message(message_data: Dict[str, Any], tool_name: str, fallback_message: Optional[str] = None):
    """Send a WebSocket message in a background thread with proper error handling."""
    websocket_sender, event_loop = get_websocket_context()
    
    if not websocket_sender or not event_loop:
        return  # No WebSocket context available
    
    # Get connection ID for logging
    connection_id = "unknown"
    try:
        context = get_user_context()
        user_id = context.get('user_id') if isinstance(context, dict) else context
        state_context = get_state_context()
        session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None
        if user_id and session_id:
            active_connection = get_active_websocket_connection(user_id, session_id)
            if active_connection:
                connection_id = active_connection[2]
                logging.info(f"🔍 WEBSOCKET DEBUG: Found active connection {connection_id} for {tool_name}")
            else:
                logging.warning(f"🔍 WEBSOCKET DEBUG: No active connection found for user {user_id}, session {session_id}")
    except Exception as e:
        logging.debug(f"Could not get connection ID for logging: {e}")
    
    logging.info(f"🔍 WEBSOCKET DEBUG: Starting background thread to send {tool_name} message via connection {connection_id}")
    
    # Start the thread immediately
    thread = threading.Thread(
        target=_send_ws_message_now,
        args=(websocket_sender, message_data, tool_name, fallback_message, connection_id),
        daemon=True,
    )
    thread.start()

def send_tool_completion(tool_name: str, output: str, status: str = "completed", tool_call_id: Optional[str] = None, tool_input: Optional[Dict] = None):
    """Send tool completion notification via WebSocket if available."""
    try:
        # Get user and session context
        context = get_user_context()
        user_id = context.get('user_id') if isinstance(context, dict) else context
        state_context = get_state_context()
        session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None
        
        # Trigger visualization update every 30s for incident RCAs
        incident_id = getattr(state_context, 'incident_id', None) if state_context else None
        if incident_id:
            try:
                global _viz_triggers
                
                if incident_id not in _viz_triggers:
                    from chat.background.visualization_triggers import VisualizationTrigger
                    _viz_triggers[incident_id] = VisualizationTrigger(incident_id)
                
                if _viz_triggers[incident_id].should_trigger():
                    from chat.background.visualization_generator import update_visualization
                    update_visualization.delay(
                        incident_id=incident_id,
                        user_id=user_id,
                        session_id=session_id,
                        force_full=False,
                        tool_calls_json=json.dumps([{
                            'tool': tool_name,
                            'output': str(output)[:MAX_TOOL_OUTPUT_CHARS]
                        }])
                    )
                    logging.info(f"[Visualization] Triggered 30s update for incident {incident_id}")
            except Exception as e:
                logging.warning(f"[Visualization] Failed to trigger update: {e}")
        
        # Try to get the Agent's websocket_sender first (preferred)
        agent_websocket_sender = None
        try:
            workflow = get_workflow_context()
            if workflow and hasattr(workflow, 'agent') and hasattr(workflow.agent, 'websocket_sender'):
                agent_websocket_sender = workflow.agent.websocket_sender
        except Exception as e:
            logging.debug(f"Could not get Agent websocket_sender: {e}")
        
        # Parse output if it's JSON and apply field-level truncation
        try:
            output_data = json.loads(output)
            # Apply field-level truncation to preserve JSON structure
            truncated_output = truncate_json_fields(output_data, max_field_length=10000)
            cleaned_output = json.dumps(truncated_output, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            # If not JSON, treat as string and truncate if too long
            cleaned_output = str(output)
            
            size_limit = 10000
            
            if len(cleaned_output) > size_limit:
                cleaned_output = cleaned_output[:size_limit] + "... [output truncated for WebSocket]"
        
        # Remove problematic characters that could break JSON
        cleaned_output = cleaned_output.replace('\x00', '').replace('\r', '').replace('\b', '').replace('\f', '')
        
        # Ensure output is valid UTF-8
        try:
            cleaned_output = cleaned_output.encode('utf-8', errors='replace').decode('utf-8')
        except Exception:
            cleaned_output = "[output encoding error]"
        
        result_data = {
            "type": "tool_result",
            "data": {
                "tool_name": str(tool_name),
                "output": cleaned_output,
                "status": str(status),
                "timestamp": str(datetime.now().isoformat()),
                "tool_call_id": tool_call_id,
                "tool_input": tool_input
            }
        }
        
        # Add session and user information if available
        if session_id:
            result_data["session_id"] = session_id
        if user_id:
            result_data["user_id"] = user_id
        
        # Use Agent's websocket_sender if available, otherwise fall back to global context
        if agent_websocket_sender:
            # Send directly using Agent's sender
            try:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # If we're in an async context, schedule the send
                        asyncio.create_task(agent_websocket_sender(result_data))
                    else:
                        # If we're in a sync context, run in thread
                        loop.run_until_complete(agent_websocket_sender(result_data))
                except RuntimeError as e:
                    if "no current event loop" in str(e):
                        # We're in a background thread without an event loop
                        # Try to get the Agent's event loop and use it
                        try:
                            workflow = get_workflow_context()
                            if workflow and hasattr(workflow, 'agent') and hasattr(workflow.agent, 'event_loop'):
                                agent_event_loop = workflow.agent.event_loop
                                if agent_event_loop and not agent_event_loop.is_closed():
                                    # Use the Agent's event loop to send the message
                                    future = asyncio.run_coroutine_threadsafe(agent_websocket_sender(result_data), agent_event_loop)
                                    future.result(timeout=5)  # Wait up to 5 seconds
                                    return
                        except Exception as agent_loop_error:
                            logging.warning(f"Failed to use Agent's event loop for {tool_name}: {agent_loop_error}")
                        
                        # Fall back to global context
                        fallback_message = f"Tool {tool_name} completed successfully"
                        send_websocket_message(result_data, tool_name, fallback_message)
                    else:
                        raise e
            except Exception as e:
                logging.warning(f"Failed to send via Agent websocket_sender for {tool_name}: {e}")
                # Fall back to global context
                fallback_message = f"Tool {tool_name} completed successfully"
                send_websocket_message(result_data, tool_name, fallback_message)
        else:
            fallback_message = f"Tool {tool_name} completed successfully"
            send_websocket_message(result_data, tool_name, fallback_message)
        
    except Exception as e:
        logging.error(f"Error in send_tool_completion for {tool_name}: {e}")
        # Don't let tool completion errors break the tool execution

def send_tool_start(tool_name: str, input_data: Any = None, tool_call_id: Optional[str] = None):
    """Send a tool start (running) notification via WebSocket if available."""
    try:
        # Safely prepare a representation of the input for transmission
        cleaned_input = None
        if input_data is not None:
            try:
                # Apply field-level truncation to input data
                if isinstance(input_data, (dict, list)):
                    truncated_input = truncate_json_fields(input_data, max_field_length=10000)
                    cleaned_input = json.dumps(truncated_input, ensure_ascii=False, default=str)
                else:
                    cleaned_input = str(input_data)
            except (TypeError, ValueError):
                cleaned_input = str(input_data)

            # Final truncation check for the serialized input
            if len(cleaned_input) > 10000:
                cleaned_input = cleaned_input[:10000] + "... [input truncated]"

        # Get user and session context
        context = get_user_context()
        user_id = context.get('user_id') if isinstance(context, dict) else context
        state_context = get_state_context()
        session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None

        result_data = {
            "type": "tool_call",
            "data": {
                "tool_name": str(tool_name),
                "input": cleaned_input,
                "status": "running",
                "timestamp": str(datetime.now().isoformat())
            }
        }
        
        # Add tool_call_id if provided
        if tool_call_id:
            result_data["data"]["tool_call_id"] = tool_call_id
        else:
            logging.warning(f"Tool call for {tool_name} does not have a tool_call_id")

        # Add session and user information if available
        if session_id:
            result_data["session_id"] = session_id
        if user_id:
            result_data["user_id"] = user_id

        send_websocket_message(result_data, tool_name)
        
    except Exception as e:
        logging.error(f"Error in send_tool_start for {tool_name}: {e}")

def send_tool_error(tool_name: str, error_msg: str, tool_call_id: Optional[str] = None):
    """Send a tool error notification via WebSocket if available."""
    try:
        cleaned_error = str(error_msg)
        # Truncate error message if too long
        if len(cleaned_error) > 10000:
            cleaned_error = cleaned_error[:10000] + "... [error truncated]"

        # Get user and session context
        context = get_user_context()
        user_id = context.get('user_id') if isinstance(context, dict) else context
        state_context = get_state_context()
        session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None

        result_data = {
            "type": "tool_error",
            "data": {
                "tool_name": str(tool_name),
                "error": cleaned_error,
                "timestamp": str(datetime.now().isoformat()),
                "tool_call_id": tool_call_id
            }
        }

        # Add session and user information if available
        if session_id:
            result_data["session_id"] = session_id
        if user_id:
            result_data["user_id"] = user_id

        send_websocket_message(result_data, tool_name)
        
    except Exception as e:
        logging.error(f"Error in send_tool_error for {tool_name}: {e}")

def with_user_context(func):
    """Decorator to inject user_id and session_id from context if not provided.
    
    FIXED: This decorator now respects explicit user_id and session_id arguments
    instead of always overriding them. This prevents mixups where user_id and 
    session_id could be swapped or overridden incorrectly.
    
    Behavior:
    - If user_id/session_id are explicitly provided, use those values
    - If not provided, fall back to thread-local context
    - Validates user_id format (must be a non-empty string)
    - Logs debug info to help track which values are being used
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Check if user_id is explicitly provided
        if 'user_id' not in kwargs or kwargs['user_id'] is None:
            context = get_user_context()
            context_user_id = context.get('user_id') if isinstance(context, dict) else context
            if context_user_id:
                kwargs['user_id'] = context_user_id
                logging.debug(f"with_user_context: Injected user_id from context: {context_user_id}")
            else:
                logging.warning(f"with_user_context: No user_id provided and none in context for {func.__name__}")
        else:
            logging.debug(f"with_user_context: Using explicit user_id: {kwargs['user_id']}")
        
        # Validate user_id format if present
        if kwargs.get('user_id'):
            user_id = kwargs['user_id']
            from utils.auth.stateless_auth import is_valid_user_id
            if not is_valid_user_id(user_id):
                logging.warning(f"with_user_context: user_id '{user_id}' is invalid (empty or not a string) for {func.__name__}")
        
        # Check if session_id is explicitly provided
        if 'session_id' not in kwargs or kwargs['session_id'] is None:
            context_state = get_state_context()
            context_session_id = context_state.session_id if context_state and hasattr(context_state, 'session_id') else None
            if context_session_id:
                kwargs['session_id'] = context_session_id
                logging.debug(f"with_user_context: Injected session_id from context: {context_session_id}")
            else:
                logging.warning(f"with_user_context: No session_id provided and none in context for {func.__name__}")
        else:
            logging.debug(f"with_user_context: Using explicit session_id: {kwargs['session_id']}")

        return func(*args, **kwargs)
    return wrapper

def with_forced_context(func):
    """Decorator that ALWAYS injects user_id and session_id from context, 
    completely removing them from the AI's view to prevent mixups.
    
    This is the preferred decorator for tools that should never have their
    user_id/session_id parameters mixed up by the AI.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # ALWAYS get user_id from context, never from AI
        context = get_user_context()
        context_user_id = context.get('user_id') if isinstance(context, dict) else context
        if context_user_id:
            kwargs['user_id'] = context_user_id
            logging.info(f"with_forced_context: Forced user_id from context: {context_user_id} for {func.__name__}")
        else:
            logging.error(f"with_forced_context: No user_id in context for {func.__name__}")
            raise ValueError(f"No user_id available in context for {func.__name__}")
        
        # ALWAYS get session_id from context, never from AI
        context_state = get_state_context()
        context_session_id = context_state.session_id if context_state and hasattr(context_state, 'session_id') else None
        if context_session_id:
            kwargs['session_id'] = context_session_id
            logging.info(f"with_forced_context: Forced session_id from context: {context_session_id} for {func.__name__}")
        else:
            logging.error(f"with_forced_context: No session_id in context for {func.__name__}")
            raise ValueError(f"No session_id available in context for {func.__name__}")
        
        # Inject incident_id if available (for RCA sessions)
        context_incident_id = context_state.incident_id if context_state and hasattr(context_state, 'incident_id') else None
        if context_incident_id:
            kwargs['incident_id'] = context_incident_id
            logging.info(f"with_forced_context: Forced incident_id from context: {context_incident_id} for {func.__name__}")
        
        # Validate user_id format
        user_id = kwargs['user_id']
        from utils.auth.stateless_auth import is_valid_user_id
        if not is_valid_user_id(user_id):
            logging.warning(f"with_forced_context: user_id '{user_id}' is invalid (empty or not a string) for {func.__name__}")

        return func(*args, **kwargs)
    return wrapper

def with_completion_notification(func):
    """Decorator to send WebSocket notifications for tool start/completion."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        tool_name = func.__name__
        # Capture a representation of the input for the start event
        input_repr = {
            "args": args,
            "kwargs": kwargs
        }

        # Generate a signature-based ID for consistent start/completion matching
        tool_input_data = {"args": args, "kwargs": kwargs}
        import hashlib
        import json
        # Use JSON serialization with sorted keys for deterministic hashing
        signature = f"{tool_name}_{json.dumps(tool_input_data, sort_keys=True, default=str)}"
        # Use longer hash (16 chars) to reduce collision risk
        signature_hash = hashlib.sha256(signature.encode()).hexdigest()[:16]
        signature_id = f"{tool_name}_{signature_hash}"
        
        # Only send start notification if no stop request
        try:
            logging.info(f"🔍 TOOL START: {tool_name} with signature_id: {signature_id}, input: {tool_input_data}")
            send_tool_start(tool_name, input_repr, signature_id)
        except Exception as start_notify_err:
            logging.warning(f"Failed to send start notification for {tool_name}: {start_notify_err}")
        
        try:
            # Execute the original function
            result = func(*args, **kwargs)
            
            # Only send completion notification if no stop request
            try:
                logging.info(f"🔍 TOOL COMPLETION: {tool_name} with signature_id: {signature_id}, input: {tool_input_data}")
                send_tool_completion(tool_name, result, "completed", signature_id, tool_input_data)
                
                # Handle post-completion actions (like GitHub commit flow)
                if tool_name == "iac_tool":
                    try:
                        result_data = json.loads(result) if isinstance(result, str) else result
                        action_performed = None
                        if isinstance(tool_input_data, dict):
                            action_performed = tool_input_data.get("action")
                        if isinstance(result_data, dict):
                            action_performed = result_data.get("action") or action_performed

                        if (
                            action_performed == "apply"
                            and isinstance(result_data, dict)
                            and "post_completion_actions" in result_data
                        ):
                            actions = result_data["post_completion_actions"]
                            if "send_github_commit_flow" in actions:
                                github_flow = actions["send_github_commit_flow"]
                                
                                # Add delay to ensure message order
                                time.sleep(1.0)
                                
                                # Then send the tool call
                                tool_call_data = {
                                    "type": "tool_call",
                                    "data": {
                                        "tool_name": "github_commit",
                                        "status": "awaiting_confirmation",
                                        "input": json.dumps({
                                            "repo": github_flow.get('repo', 'user/repository'),
                                            "commit_message": github_flow.get('commit_message', 'Apply Terraform changes'),
                                            "branch": github_flow.get('branch', 'main')
                                        }),
                                        "timestamp": str(time.time())
                                    }
                                }
                                send_websocket_message(tool_call_data, "github_commit_tool")
                                logging.info(f"Sent GitHub commit flow for repo: {github_flow.get('repo')}")
                    except Exception as post_action_error:
                        logging.warning(f"Failed to handle post-completion actions for {tool_name}: {post_action_error}")
                        
            except Exception as notification_error:
                logging.warning(f"Failed to send completion notification for {tool_name}: {notification_error}")
                # Don't let notification errors break the tool
            
            return result
            
        except Exception as e:
            # Send error notification (non-blocking) - but check for stop request first
            try:
                # Use input signature for matching instead of unreliable tool_call_id for parallel execution
                tool_input_data = {"args": args, "kwargs": kwargs}
                # Generate a signature-based ID for better matching
                import hashlib
                import json
                # Use JSON serialization with sorted keys for deterministic hashing
                signature = f"{tool_name}_{json.dumps(tool_input_data, sort_keys=True, default=str)}"
                # Use longer hash (16 chars) to reduce collision risk
                signature_hash = hashlib.sha256(signature.encode()).hexdigest()[:16]
                signature_id = f"{tool_name}_{signature_hash}"
                
                send_tool_error(tool_name, str(e), signature_id)
            except Exception as notification_error:
                logging.warning(f"Failed to send error notification for {tool_name}: {notification_error}")
                # Don't let notification errors mask the original error
            
            raise  # Re-raise the original tool exception
            
    return wrapper

def get_current_tool_call_id(tool_name: str = None, tool_kwargs: dict = None):
    """Get the current tool call ID from the tool capture context.
    
    For parallel tool calls, if tool_name and tool_kwargs are provided, matches by signature.
    Otherwise, returns the most recent running call (legacy behavior).
    
    Args:
        tool_name: Name of the tool (e.g., 'cloud_exec')
        tool_kwargs: Tool input kwargs to compute signature for matching
    """
    tool_capture = get_tool_capture()
    if not tool_capture or not hasattr(tool_capture, 'current_tool_calls'):
        return None
    
    # Get current thread ID to identify which tool call belongs to this execution
    import threading
    current_thread_id = threading.get_ident()
    
    # Look for a tool call that's still running and associated with this thread
    # Since ToolContextCapture doesn't track thread IDs, we'll use a different approach:
    # Return the first running tool call we find (this is imperfect but should work for most cases)
    running_calls = [
        call_id for call_id, call_info in tool_capture.current_tool_calls.items()
        if call_info.get('status') != 'completed'
    ]
    
    if not running_calls:
        logging.warning(f"🔍 get_current_tool_call_id: No running tool calls found")
        return None
    
    # If tool_name and tool_kwargs are provided, match by signature (for parallel calls)
    if tool_name and tool_kwargs:
        # Compute the signature the same way ToolContextCapture does (normalized JSON with sorted keys)
        import json
        try:
            normalized_input = json.dumps(tool_kwargs, sort_keys=True) if isinstance(tool_kwargs, dict) else str(tool_kwargs)
        except (TypeError, ValueError):
            normalized_input = str(tool_kwargs)
        current_signature = f"{tool_name}_{normalized_input}"
        
        # Find the running call with matching signature
        for call_id in running_calls:
            call_info = tool_capture.current_tool_calls[call_id]
            if call_info.get('signature') == current_signature:
                logging.info(f"🔍 get_current_tool_call_id: Matched by signature: {call_id} (signature: {current_signature})")
                return call_id
        
        # If no match by signature, log warning and fall back to most recent
        logging.warning(f"🔍 get_current_tool_call_id: No signature match for {current_signature}, falling back to most recent")
    
    # Fallback: Sort by start time to get the most recent one
    sorted_calls = sorted(
        running_calls,
        key=lambda call_id: tool_capture.current_tool_calls[call_id].get('start_time', datetime.min),
        reverse=True  # Most recent first
    )
    selected_call_id = sorted_calls[0]
    logging.info(f"🔍 get_current_tool_call_id: Found {len(running_calls)} running calls, selected most recent: {selected_call_id}")
    return selected_call_id

__all__ = [
    "iac_tool",
    "cloud_exec",
    "web_search",
    "get_cloud_tools",
    "set_user_context",
    "get_user_context",
    "get_state_context",
    "get_workflow_context",
    "set_websocket_context",
    "get_websocket_context",
    "send_tool_completion",
    "send_tool_start",
    "send_tool_error",
    "send_websocket_message",
    "with_completion_notification",
    "set_provider_preference",
    "get_selected_project_id",
    "set_selected_project_id",
    "set_tool_capture",
    "get_tool_capture",
    "get_current_tool_call_id",
    "determine_target_provider_from_context",
]

# Import MCP functionality from separate module
from .mcp_tools import (
    REAL_MCP_ENABLED,
    REAL_MCP_SERVER_PATHS,
    RealMCPServerManager,
    _mcp_manager,
    clear_credentials_cache,
    get_user_cloud_credentials,
    run_async_in_thread,
    get_real_mcp_tools_for_user,
    create_mcp_langchain_tools,
    _langchain_tools_cache,
    _langchain_tools_cache_expiry,
    LANGCHAIN_TOOLS_CACHE_DURATION
)

def get_cloud_tools():
    """Get all cloud management tools including both Aurora native tools and REAL MCP tools."""
    # Import required classes at function start to avoid scope issues
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field
    
    # Get tool capture from thread-local context FIRST (before cache check)
    # This ensures consistent behavior whether cached or not
    tool_capture = get_tool_capture()
    
    # Check if we have cached LangChain tools for this user
    user_context = get_user_context()
    user_id = user_context.get('user_id') if isinstance(user_context, dict) else user_context
    state_context = get_state_context()
    mode = None
    if state_context and hasattr(state_context, 'mode'):
        mode = getattr(state_context, 'mode', None)
    if mode is None:
        mode = get_mode_from_context()
    mode_suffix = (mode or 'agent').lower()

    # Create a cache key that accurately reflects the *specific* tool_capture instance (or lack thereof)
    # - When no tool_capture is active we can safely cache per-user
    # - When a tool_capture **is** active we additionally key on the `id()` of the object so each
    #   session gets its own wrapped functions that close over the *right* capture instance.
    if tool_capture is None:
        cache_key = f"{user_id}:nocapture:{mode_suffix}"
    else:
        cache_key = f"{user_id}:capture:{id(tool_capture)}:{mode_suffix}"
    
    if user_id:
        current_time = time.time()
        if (
            cache_key in _langchain_tools_cache and
            cache_key in _langchain_tools_cache_expiry and
            current_time < _langchain_tools_cache_expiry[cache_key]
        ):
            logging.info(
                f"Using fully cached LangChain tools for user {user_id} (cache key: {cache_key})"
            )
            cached_tools = _langchain_tools_cache[cache_key]
            # Important: Return a copy to avoid modifications affecting the cache
            return list(cached_tools)
    
    # Create wrapper function to capture tool results
    INTERNAL_CONTEXT_KEYS = {
        "user_id",
        "session_id",
        "provider_preference",
        "event_loop",
        "websocket_sender",
        "timeout",
        "state",
    }

    def _sanitize_kwargs_for_signature(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in kwargs.items() if k not in INTERNAL_CONTEXT_KEYS}

    def wrap_func_with_capture(func, tool_name):
        """Wrap a function to capture its execution for LLM context."""
        if not tool_capture:
            return func
            
        @wraps(func)
        def wrapped_func(**kwargs):
            exec_lock = getattr(tool_capture, 'execution_lock', None)
            acquired = False
            try:
                if exec_lock:
                    exec_lock.acquire()
                    acquired = True
                
                # FIX for OpenAI models: Update signature before execution
                # OpenAI registers tools with placeholder signatures, then populates args during execution
                # We need to update the signature now that we have the actual kwargs
                if tool_capture:
                    signature_payload = _sanitize_kwargs_for_signature(kwargs)
                    try:
                        serialized_payload = json.dumps(signature_payload, sort_keys=True)
                    except (TypeError, ValueError):
                        serialized_payload = str(signature_payload)
                    tool_signature = f"{tool_name}_{serialized_payload}"
                    
                    # Update the signature for any tool call with placeholder signature
                    with tool_capture.lock:
                        for call_id, call_info in tool_capture.current_tool_calls.items():
                            if (call_info.get('signature') == f"{tool_name}___placeholder__" and
                                call_info.get('tool_name') == tool_name and
                                not call_info.get('completed')):
                                # This is likely the tool we're about to execute - update its signature
                                call_info['signature'] = tool_signature
                                call_info['input'] = kwargs
                                logging.info(f"Updated placeholder signature for {call_id} to {tool_signature}")
                                break
                
                # Execute the original function
                result = func(**kwargs)
                
                # FIXED: Find the matching tool call ID by signature match instead of just "incomplete" status
                matching_tool_call_id = None
                if tool_capture:
                    # Create signature to match against stored tool calls
                    logging.info(f"TOOL CAPTURE: KWARGS: {kwargs}")
                    signature_payload = _sanitize_kwargs_for_signature(kwargs)
                    try:
                        serialized_payload = json.dumps(signature_payload, sort_keys=True)
                    except (TypeError, ValueError):
                        serialized_payload = str(signature_payload)
                    tool_signature = f"{tool_name}_{serialized_payload}"
                    
                    # Look for a tool call that matches this exact signature
                    # DON'T skip completed calls here - we need to match them correctly!
                    # The 'completed' flag just prevents cleanup race conditions
                    logging.info(f"TOOL CAPTURE: Tool capture instance found: {tool_capture}")
                    logging.info(f"TOOL CAPTURE: Tool capture instance found: {tool_capture.current_tool_calls}")
                    for call_id, call_info in tool_capture.current_tool_calls.items():
                        logging.info(f"TOOL CAPTURE: Call info: {call_info}")
                        logging.info(f"TOOL CAPTURE: Call signature right side: {tool_signature}")
                        logging.info(f"TOOL CAPTURE: Call result: {result}")
                        if call_info.get('signature') == tool_signature:
                            matching_tool_call_id = call_id
                            logging.info(f"Found matching tool call for completion: {matching_tool_call_id}")
                            break
                    
                    # Fallback – match by tool_name + command (provider may be missing)
                    if not matching_tool_call_id:
                        for call_id, call_info in tool_capture.current_tool_calls.items():
                            if call_info.get('tool_name') != tool_name:
                                continue
                            ci_input = call_info.get('input', {}) or {}
                            # Require command to match exactly; provider is optional
                            if ci_input.get('command') == kwargs.get('command'):
                                matching_tool_call_id = call_id
                                logging.info(
                                    "Matched tool call by command fallback: %s (tool_name=%s, command=%s)",
                                    matching_tool_call_id,
                                    tool_name,
                                    ci_input.get('command'),
                                )
                                break

                    # As a last resort, match by oldest incomplete call for sequential execution (OpenAI)
                    # For parallel execution (Gemini), signature matching should have already succeeded
                    if not matching_tool_call_id:
                        candidate_ids = [
                            (call_id, call_info.get('start_time'))
                            for call_id, call_info in tool_capture.current_tool_calls.items()
                            if call_info.get('tool_name') == tool_name and not call_info.get('completed')
                        ]
                        
                        if len(candidate_ids) == 1:
                            # Only one candidate - safe to use
                            matching_tool_call_id = candidate_ids[0][0]
                            call_info = tool_capture.current_tool_calls[matching_tool_call_id]
                            call_info['input'] = signature_payload
                            call_info['signature'] = tool_signature
                            logging.info(
                                "Matched tool call by single incomplete candidate: %s (updated signature)",
                                matching_tool_call_id,
                            )
                        elif len(candidate_ids) > 1:
                            # Multiple candidates - for sequential execution, pick the oldest
                            # This handles OpenAI's sequential execution pattern
                            candidate_ids.sort(key=lambda x: x[1] if x[1] else datetime.min)
                            matching_tool_call_id = candidate_ids[0][0]
                            call_info = tool_capture.current_tool_calls[matching_tool_call_id]
                            call_info['input'] = signature_payload
                            call_info['signature'] = tool_signature
                            logging.warning(
                                f"SEQUENTIAL FALLBACK: Found {len(candidate_ids)} incomplete {tool_name} calls, "
                                f"matched to oldest: {matching_tool_call_id}. "
                                f"This is expected for OpenAI sequential execution."
                            )

                logging.info(f"Matching tool call id: {matching_tool_call_id} and tool_capture: {tool_capture}")
                if matching_tool_call_id and tool_capture:
                    # Call capture_tool_end to ensure ToolMessage is added to collected_tool_messages
                    # This is essential for cancelled chats where get_collected_tool_messages() is called
                    # Some tools (cloud_exec, iac_tool with action=apply) call this themselves, but other
                    # invocations still need us to capture completion here
                    # The wrapper must ensure it's always called for consistency
                    # Check if capture_tool_end was already called (completed=True means it was)
                    with tool_capture.lock:
                        already_captured = tool_capture.current_tool_calls.get(matching_tool_call_id, {}).get('completed', False)
                    
                    if not already_captured:
                        try:
                            output_str = json.dumps(result) if isinstance(result, dict) else str(result)
                            tool_capture.capture_tool_end(matching_tool_call_id, output_str, is_error=False)
                            logging.info(f"Wrapper called capture_tool_end for {matching_tool_call_id}")
                        except Exception as capture_error:
                            logging.error(f"Failed to capture tool end in wrapper: {capture_error}")
                    else:
                        logging.debug(f"Skipping capture_tool_end for {matching_tool_call_id} - already captured by tool implementation")
                    
                    # Now clean up by removing from current_tool_calls after successful matching and capture
                    if matching_tool_call_id in tool_capture.current_tool_calls:
                        with tool_capture.lock:
                            if matching_tool_call_id in tool_capture.current_tool_calls:
                                del tool_capture.current_tool_calls[matching_tool_call_id]
                                logging.info(f"Cleaned up completed tool call {matching_tool_call_id} from tracking after successful match")
                else:
                    logging.warning(f"No matching tool call found for {tool_name} completion - tool tracking may have been lost")
                    # Note: send_tool_completion was already called above (line 588), so the WebSocket
                    # notification should still be sent even if backend tracking is lost
                
                return result
            except Exception as e:
                # Find matching tool call for error reporting
                matching_tool_call_id = None
                if tool_capture:
                    # Try signature matching first (for consistency with success path)
                    signature_payload = _sanitize_kwargs_for_signature(kwargs)
                    try:
                        serialized_payload = json.dumps(signature_payload, sort_keys=True)
                    except (TypeError, ValueError):
                        serialized_payload = str(signature_payload)
                    tool_signature = f"{tool_name}_{serialized_payload}"
                    
                    for call_id, call_info in tool_capture.current_tool_calls.items():
                        if call_info.get('signature') == tool_signature and not call_info.get('completed'):
                            matching_tool_call_id = call_id
                            logging.info(f"Matched error to tool call by signature: {matching_tool_call_id}")
                            break
                    
                    # Fallback: Only match if there's exactly ONE incomplete call for this tool
                    # This prevents parallel tool calls from sharing the same error tracking
                    if not matching_tool_call_id:
                        incomplete_calls = [
                            call_id for call_id, call_info in tool_capture.current_tool_calls.items() 
                            if call_info.get('tool_name') == tool_name and not call_info.get('completed', False)
                        ]
                        
                        if len(incomplete_calls) == 1:
                            matching_tool_call_id = incomplete_calls[0]
                            logging.info(f"Found single incomplete tool call for error: {matching_tool_call_id}")
                        elif len(incomplete_calls) > 1:
                            logging.error(
                                f"PARALLEL TOOL CALL ERROR: Found {len(incomplete_calls)} incomplete {tool_name} calls "
                                f"during error handling. Cannot safely match error to specific call. IDs: {incomplete_calls}"
                            )
                        else:
                            logging.warning(f"No incomplete tool calls found for {tool_name} error")
                
                if matching_tool_call_id and tool_capture:
                    # CRITICAL: Call capture_tool_end for error case to ensure ToolMessage is added
                    # This is essential for cancelled chats where get_collected_tool_messages() is called
                    # Check if capture_tool_end was already called (completed=True means it was)
                    with tool_capture.lock:
                        already_captured = tool_capture.current_tool_calls.get(matching_tool_call_id, {}).get('completed', False)
                    
                    if not already_captured:
                        try:
                            error_str = str(e)
                            tool_capture.capture_tool_end(matching_tool_call_id, error_str, is_error=True)
                            logging.info(f"Wrapper called capture_tool_end for error in {matching_tool_call_id}")
                        except Exception as capture_error:
                            logging.error(f"Failed to capture tool error in wrapper: {capture_error}")
                    else:
                        logging.debug(f"Skipping capture_tool_end for error in {matching_tool_call_id} - already captured by tool implementation")
                    
                    # Now clean up by removing from current_tool_calls after successful error matching and capture
                    if matching_tool_call_id in tool_capture.current_tool_calls:
                        with tool_capture.lock:
                            if matching_tool_call_id in tool_capture.current_tool_calls:
                                del tool_capture.current_tool_calls[matching_tool_call_id]
                                logging.info(f"Cleaned up errored tool call {matching_tool_call_id} from tracking after successful match")
                raise
            finally:
                if acquired and exec_lock:
                    exec_lock.release()

        return wrapped_func
    
    # Create Aurora native tools with optional capture wrapping
    tools = []
    
    # Create wrapper for cloud_exec to hide internal parameters from AI
    def cloud_exec_wrapper(provider: str, command: str, output_file: Optional[str] = None, **kwargs) -> str:
        """Execute cloud CLI commands. Provider and command are required. Use output_file to save raw output to a file (useful for kubeconfig)."""
        # Extract the injected context parameters and pass them to the real function
        user_id = kwargs.get('user_id')
        session_id = kwargs.get('session_id')
        provider_preference = kwargs.get('provider_preference')
        timeout = kwargs.get('timeout')
        
        return cloud_exec(provider, command, user_id=user_id, session_id=session_id, 
                         provider_preference=provider_preference, timeout=timeout, output_file=output_file)
    
    # Set the name to match what the system prompt expects
    cloud_exec_wrapper.__name__ = "cloud_exec"
    
    # Import on-prem kubectl tool
    from chat.backend.agent.tools.kubectl_onprem_tool import on_prem_kubectl
    
    # List of (function, name) tuples
    tool_functions = [
        (run_iac_tool, "iac_tool"),
        (github_commit, "github_commit"),
        (github_rca, "github_rca"),
        (github_fix, "github_fix"),
        (github_apply_fix, "github_apply_fix"),
        (cloud_exec_wrapper, "cloud_exec"),
        (terminal_exec, "terminal_exec"),
        (tailscale_ssh, "tailscale_ssh"),
        (confluence_runbook_parse, "confluence_runbook_parse"),
        (on_prem_kubectl, "on_prem_kubectl"),
        (analyze_zip_file, "analyze_zip_file"),
        # (web_search, "web_search"),  # Moved to dedicated registration below with explicit args_schema
    ]
    
    # Process Aurora native tools
    for func, name in tool_functions:
        # Apply forced context wrapper for critical tools that should never have parameters mixed up
        if name in ['iac_tool', 'github_commit', 'github_fix', 'github_apply_fix']:
            context_wrapped = with_forced_context(func)
            logging.info(f"Applied with_forced_context decorator to {name}")
        else:
            # Apply user context wrapper for other tools
            context_wrapped = with_user_context(func)
            logging.info(f"Applied with_user_context decorator to {name}")
        
        # Apply completion notification wrapper for WebSocket updates
        notification_wrapped = with_completion_notification(context_wrapped)

        # Apply capture wrapper if tool_capture is available
        if tool_capture:
            final_func = wrap_func_with_capture(notification_wrapped, name)
        else:
            final_func = notification_wrapped

        # Ensure the callable exposes the intended tool name
        final_func.__name__ = name
            
        # Create StructuredTool with proper args_schema for tools with complex parameters
        if name == 'github_commit':
            tool = StructuredTool.from_function(
                func=final_func,
                name=name,
                description="Commit and push Terraform files to a GitHub repository. Parameters: repo (string, required) - repository in 'owner/repo' format, commit_message (string, required) - commit message, branch (string, optional, default='main') - target branch, push (boolean, optional, default=true) - whether to push.",
                args_schema=GitHubCommitArgs
            )
        elif name == 'github_rca':
            tool = StructuredTool.from_function(
                func=final_func,
                name=name,
                description=(
                    "Unified GitHub investigation tool for Root Cause Analysis. "
                    "Actions: 'deployment_check' (GitHub Actions workflow runs), "
                    "'commits' (recent commits with timeline correlation), "
                    "'diff' (file changes for a specific commit), "
                    "'pull_requests' (merged PRs in time window). "
                    "Auto-resolves repository from Knowledge Base or connected repo if not specified. "
                    "Pass incident_time (ISO 8601) for automatic time window correlation."
                ),
                args_schema=GitHubRCAArgs
            )
        elif name == 'github_fix':
            tool = StructuredTool.from_function(
                func=final_func,
                name=name,
                description=(
                    "Suggest a code fix for an identified issue during RCA. "
                    "Use this when you identify a specific code change that would fix the root cause. "
                    "The fix is stored for user review before being applied. "
                    "Parameters: file_path (path in repo), suggested_content (complete fixed file), "
                    "fix_description (what this fix does), root_cause_summary (why this change is needed). "
                    "Optional: repo (owner/repo format), commit_message, branch."
                ),
                args_schema=GitHubFixArgs
            )
        elif name == 'github_apply_fix':
            tool = StructuredTool.from_function(
                func=final_func,
                name=name,
                description=(
                    "Apply an approved fix suggestion by creating a branch and PR. "
                    "Use this after the user has reviewed and approved a fix suggestion. "
                    "Parameters: suggestion_id (ID of the fix suggestion to apply), "
                    "use_edited_content (boolean, default true - use user-edited content if available), "
                    "target_branch (optional base branch for PR, defaults to main)."
                ),
                args_schema=GitHubApplyFixArgs
            )
        elif name == "confluence_runbook_parse":
            tool = StructuredTool.from_function(
                func=final_func,
                name=name,
                description="Fetch and parse a Confluence runbook into markdown and steps for LLM use. Parameter: page_url (string, required).",
                args_schema=ConfluenceRunbookArgs,
            )
        else:
            tool = StructuredTool.from_function(final_func)
        tools.append(tool)
    
    # Add analyze_zip tool for explicit use only (filtered elsewhere if not referenced)
    tools.append(StructuredTool.from_function(
        func=analyze_zip_file,
        name="analyze_zip_file",
        description="Analyze a ZIP attachment: list, extract a file, or detect project structure",
    ))

    # Add RAG indexer for ZIPs
    tools.append(StructuredTool.from_function(
        func=rag_index_zip,
        name="rag_index_zip",
        description=(
            "Index code/text files from an uploaded ZIP into the RAG store (Weaviate). "
            "Arguments: attachment_index (int)=0, max_files (int)=200, max_file_bytes (int)=750000, "
            "include_patterns (list[str]) and exclude_dirs (list[str])."
        ),
        args_schema=RAGIndexZipArgs,
    ))
    
    # Add Knowledge Base search tool for authenticated users
    if user_id:
        try:
            from chat.backend.agent.tools.knowledge_base_search_tool import (
                knowledge_base_search,
                KnowledgeBaseSearchArgs,
                KNOWLEDGE_BASE_SEARCH_DESCRIPTION,
            )

            context_wrapped_kb = with_user_context(knowledge_base_search)
            notification_wrapped_kb = with_completion_notification(context_wrapped_kb)
            if tool_capture:
                final_kb_func = wrap_func_with_capture(notification_wrapped_kb, "knowledge_base_search")
            else:
                final_kb_func = notification_wrapped_kb

            tools.append(StructuredTool.from_function(
                func=final_kb_func,
                name="knowledge_base_search",
                description=KNOWLEDGE_BASE_SEARCH_DESCRIPTION,
                args_schema=KnowledgeBaseSearchArgs,
            ))
            logging.info(f"Added knowledge_base_search tool for user {user_id}")
        except Exception as e:
            logging.warning(f"Failed to add knowledge_base_search tool: {e}")


    # Add Splunk tools if connected
    if user_id and is_splunk_connected(user_id):
        # search_splunk tool
        context_wrapped_splunk = with_user_context(search_splunk)
        notification_wrapped_splunk = with_completion_notification(context_wrapped_splunk)
        if tool_capture:
            final_splunk_func = wrap_func_with_capture(notification_wrapped_splunk, "search_splunk")
        else:
            final_splunk_func = notification_wrapped_splunk

        tools.append(StructuredTool.from_function(
            func=final_splunk_func,
            name="search_splunk",
            description=(
                "Execute SPL (Splunk Processing Language) queries to search logs in Splunk. "
                "Use this to investigate issues by querying log data. "
                "First use list_splunk_indexes to discover available indexes, then construct targeted queries. "
                "Example: search_splunk(query='index=main error | stats count by host', earliest_time='-1h')"
            ),
            args_schema=SplunkSearchArgs,
        ))

        # list_splunk_indexes tool
        context_wrapped_indexes = with_user_context(list_splunk_indexes)
        notification_wrapped_indexes = with_completion_notification(context_wrapped_indexes)
        if tool_capture:
            final_indexes_func = wrap_func_with_capture(notification_wrapped_indexes, "list_splunk_indexes")
        else:
            final_indexes_func = notification_wrapped_indexes

        tools.append(StructuredTool.from_function(
            func=final_indexes_func,
            name="list_splunk_indexes",
            description="List available Splunk indexes to discover what log data is available for searching.",
            args_schema=SplunkListIndexesArgs,
        ))

        # list_splunk_sourcetypes tool
        context_wrapped_sourcetypes = with_user_context(list_splunk_sourcetypes)
        notification_wrapped_sourcetypes = with_completion_notification(context_wrapped_sourcetypes)
        if tool_capture:
            final_sourcetypes_func = wrap_func_with_capture(notification_wrapped_sourcetypes, "list_splunk_sourcetypes")
        else:
            final_sourcetypes_func = notification_wrapped_sourcetypes

        tools.append(StructuredTool.from_function(
            func=final_sourcetypes_func,
            name="list_splunk_sourcetypes",
            description="List available Splunk sourcetypes. Optionally filter by index to see what log types exist.",
            args_schema=SplunkListSourcetypesArgs,
        ))

        logging.info(f"Added 3 Splunk tools for user {user_id}")
    else:
        logging.debug(f"Splunk tools not added - user {user_id} not connected to Splunk")

    # Add Dynatrace tool if connected
    if user_id and is_dynatrace_connected(user_id):
        context_wrapped_dt = with_user_context(query_dynatrace)
        notification_wrapped_dt = with_completion_notification(context_wrapped_dt)
        final_dt_func = wrap_func_with_capture(notification_wrapped_dt, "query_dynatrace") if tool_capture else notification_wrapped_dt

        tools.append(StructuredTool.from_function(
            func=final_dt_func,
            name="query_dynatrace",
            description=(
                "Query Dynatrace for problems, logs, metrics, or monitored entities. "
                "Set resource_type to 'problems', 'logs', 'metrics', or 'entities'. "
                "Examples: query_dynatrace(resource_type='problems', query='status(\"open\")', time_from='now-1h') "
                "or query_dynatrace(resource_type='metrics', query='builtin:host.cpu.usage', time_from='now-30m')"
            ),
            args_schema=QueryDynatraceArgs,
        ))
        logging.info(f"Added Dynatrace tool for user {user_id}")

    # Add Bitbucket tools if connected
    try:
        from .bitbucket import is_bitbucket_connected

        if user_id and is_bitbucket_connected(user_id):
            from .bitbucket import (
                bitbucket_repos, BitbucketReposArgs,
                bitbucket_branches, BitbucketBranchesArgs,
                bitbucket_pull_requests, BitbucketPullRequestsArgs,
                bitbucket_issues, BitbucketIssuesArgs,
                bitbucket_pipelines, BitbucketPipelinesArgs,
            )

            _bb_tools = [
                (bitbucket_repos, "bitbucket_repos", BitbucketReposArgs,
                 "Manage Bitbucket repositories, files, and code. Actions: list_repos, get_repo, "
                 "get_file_contents, create_or_update_file, delete_file, get_directory_tree, "
                 "search_code, list_workspaces, get_workspace. Workspace and repo auto-resolve "
                 "from saved selection if not specified."),
                (bitbucket_branches, "bitbucket_branches", BitbucketBranchesArgs,
                 "Manage Bitbucket branches and view commits/diffs. Actions: list_branches, create_branch, "
                 "delete_branch, list_commits, get_commit, get_diff, compare."),
                (bitbucket_pull_requests, "bitbucket_pull_requests", BitbucketPullRequestsArgs,
                 "Manage Bitbucket pull requests. Actions: list_prs, get_pr, create_pr, update_pr, "
                 "merge_pr, approve_pr, unapprove_pr, decline_pr, list_pr_comments, add_pr_comment, "
                 "get_pr_diff, get_pr_activity."),
                (bitbucket_issues, "bitbucket_issues", BitbucketIssuesArgs,
                 "Manage Bitbucket issues. Actions: list_issues, get_issue, create_issue, "
                 "update_issue, list_issue_comments, add_issue_comment."),
                (bitbucket_pipelines, "bitbucket_pipelines", BitbucketPipelinesArgs,
                 "Manage Bitbucket Pipelines CI/CD. Actions: list_pipelines, get_pipeline, "
                 "trigger_pipeline, stop_pipeline, list_pipeline_steps, get_step_log, get_pipeline_step."),
            ]
            for _func, _name, _schema, _desc in _bb_tools:
                _ctx = with_user_context(_func)
                _notif = with_completion_notification(_ctx)
                _final = wrap_func_with_capture(_notif, _name) if tool_capture else _notif
                tools.append(StructuredTool.from_function(
                    func=_final, name=_name, description=_desc, args_schema=_schema))
            logging.info(f"Added {len(_bb_tools)} Bitbucket tools for user {user_id}")
    except Exception as e:
        logging.warning(f"Failed to add Bitbucket tools: {e}")

    # Add Confluence search tools if enabled
    try:
        from utils.flags.feature_flags import is_confluence_enabled

        if is_confluence_enabled() and user_id:
            _confluence_tools = [
                (confluence_search_similar, "confluence_search_similar", ConfluenceSearchSimilarArgs,
                 "Search Confluence for pages related to an incident (postmortems, RCA docs). "
                 "Pass keywords, optional service_name and error_message. Returns matching pages with excerpts."),
                (confluence_search_runbooks, "confluence_search_runbooks", ConfluenceSearchRunbookArgs,
                 "Search Confluence for runbooks / playbooks / SOPs for a given service. "
                 "Pass service_name and optional operation (e.g. 'restart', 'failover')."),
                (confluence_fetch_page, "confluence_fetch_page", ConfluenceFetchPageArgs,
                 "Fetch a Confluence page by ID and return its content as markdown. "
                 "Use after search to read full page details."),
            ]
            for _func, _name, _schema, _desc in _confluence_tools:
                _ctx = with_user_context(_func)
                _notif = with_completion_notification(_ctx)
                _final = wrap_func_with_capture(_notif, _name) if tool_capture else _notif
                tools.append(StructuredTool.from_function(
                    func=_final,
                    name=_name,
                    description=_desc,
                    args_schema=_schema,
                ))
            logging.info(f"Added 3 Confluence search tools for user {user_id}")
    except Exception as e:
        logging.warning(f"Failed to add Confluence search tools: {e}")

    # Add Coroot observability tools if connected
    try:
        if user_id and is_coroot_connected(user_id):
            _coroot_tools = [
                (coroot_get_incidents, "coroot_get_incidents", CorootGetIncidentsArgs,
                 "List recent incidents from Coroot (eBPF-powered observability) with RCA summaries, severity, "
                 "root cause, and fix suggestions. Use this first when investigating production issues."),
                (coroot_get_incident_detail, "coroot_get_incident_detail", CorootGetIncidentDetailArgs,
                 "Get full detail for a specific Coroot incident including SLO data, RCA analysis, and propagation map."),
                (coroot_get_applications, "coroot_get_applications", CorootGetApplicationsArgs,
                 "List all applications with health status from eBPF kernel-level instrumentation: SLO, CPU throttling, "
                 "memory OOM kills, TCP connection failures, network retransmissions, HTTP errors, latency, DNS issues."),
                (coroot_get_app_detail, "coroot_get_app_detail", CorootGetAppDetailArgs,
                 "Get full audit reports for one application (22 report types, 35+ health checks from eBPF). "
                 "Detects kernel-level issues invisible to app logs: OOM kills, TCP failures, disk I/O saturation, "
                 "CPU throttling, DNS errors, network packet loss, DB connection pool exhaustion."),
                (coroot_get_app_logs, "coroot_get_app_logs", CorootGetAppLogsArgs,
                 "Fetch logs for a SINGLE application (requires app_id). Use this when you already know which app to "
                 "investigate. Filter by severity (Error/Warning/Info) and message content. "
                 "Returns timestamps, messages, attributes, and trace IDs for correlation."),
                (coroot_get_traces, "coroot_get_traces", CorootGetTracesArgs,
                 "Search distributed traces across all applications or look up a specific trace by ID. "
                 "Filter by service name and error status. Shows full span trees with timing."),
                (coroot_get_service_map, "coroot_get_service_map", CorootGetServiceMapArgs,
                 "Get the service dependency map auto-discovered via eBPF TCP connection tracking. Shows all "
                 "applications, upstream/downstream connections, request rates, latency, and connection health."),
                (coroot_query_metrics, "coroot_query_metrics", CorootQueryMetricsArgs,
                 "Execute PromQL queries against Coroot's eBPF-collected metrics. All metrics are gathered at the "
                 "kernel level without exporters: CPU, memory, TCP connections, retransmissions, network RTT, "
                 "HTTP requests, DNS, disk I/O, DB query latency. "
                 "Example: rate(container_net_tcp_failed_connects_total[5m])"),
                (coroot_get_deployments, "coroot_get_deployments", CorootGetDeploymentsArgs,
                 "List recent deployments to correlate with incidents. Shows deployment status and age."),
                (coroot_get_nodes, "coroot_get_nodes", CorootGetNodesArgs,
                 "List all infrastructure nodes with kernel-level CPU, memory, disk I/O, and network health."),
                (coroot_get_overview_logs, "coroot_get_overview_logs", CorootGetOverviewLogsArgs,
                 "Search logs cluster-wide across ALL applications (no app_id needed). Use this when you don't yet know "
                 "which app is failing. Set kubernetes_only=true to get K8s events (OOMKilled, Evicted, CrashLoopBackOff, "
                 "FailedScheduling). Use coroot_get_app_logs instead when you already know the target app."),
                (coroot_get_node_detail, "coroot_get_node_detail", CorootGetNodeDetailArgs,
                 "Get full audit report for a specific node (CPU breakdown, memory breakdown, disk per-mount, "
                 "network per-interface, GPU). Use after coroot_get_nodes shows a WARNING/CRITICAL node."),
                (coroot_get_costs, "coroot_get_costs", CorootGetCostsArgs,
                 "Get cost breakdown per node and per application, plus right-sizing recommendations. "
                 "Cost spikes correlate with autoscaling issues, memory leaks (OOMing pods), retry storms. "
                 "Shows current vs recommended CPU/memory allocations."),
                (coroot_get_risks, "coroot_get_risks", CorootGetRisksArgs,
                 "Get security and availability risks: single-instance apps, single-AZ deployments, spot-only "
                 "workloads, exposed database ports. Explains why services are vulnerable to outages."),
            ]
            for _func, _name, _schema, _desc in _coroot_tools:
                _ctx = with_user_context(_func)
                _notif = with_completion_notification(_ctx)
                _final = wrap_func_with_capture(_notif, _name) if tool_capture else _notif
                tools.append(StructuredTool.from_function(
                    func=_final,
                    name=_name,
                    description=_desc,
                    args_schema=_schema,
                ))
            logging.info(f"Added {len(_coroot_tools)} Coroot observability tools for user {user_id}")
        else:
            logging.debug(f"Coroot tools not added - user {user_id} not connected to Coroot")
    except Exception as e:
        logging.warning(f"Failed to add Coroot observability tools (treating as not connected): {e}")

    logging.info(f"Created {len(tools)} Aurora native tools")
    
    # Add real MCP tools if available (simplified approach)
    try:
        # Get user context to determine which MCP tools to include
        user_context = get_user_context()
        user_id = user_context.get('user_id') if isinstance(user_context, dict) else user_context
        
        if user_id:
            # Get real MCP tools from all providers with safe timeout
            logging.info(f"Fetching MCP tools for user {user_id}")
            
            try:
                # Use a longer timeout for Azure MCP operations which can be slow
                real_mcp_tools = run_async_in_thread(get_real_mcp_tools_for_user(user_id), timeout=90)
            except Exception as e:
                logging.warning(f" MCP tool retrieval failed: {str(e)}")
                real_mcp_tools = []
            
            if real_mcp_tools:
                # Convert MCP tools to LangChain tools using the new module
                mcp_tools = create_mcp_langchain_tools(
                    real_mcp_tools, 
                    tool_capture=tool_capture,
                    send_tool_start=send_tool_start,
                    send_tool_completion=send_tool_completion,
                    send_tool_error=send_tool_error,
                    run_async_in_thread=run_async_in_thread
                )
                tools.extend(mcp_tools)
                logging.info(f"Added {len(mcp_tools)} MCP tools for user {user_id}")
            else:
                logging.warning(f"No MCP tools returned for user {user_id} - this may indicate a timeout or error")
                    
    except Exception as e:
        logging.error(f"Error adding real MCP tools: {str(e)}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        # Continue with native tools even if MCP fails
    
    # Add web_search tool with explicit args_schema so LLM sees full parameter schema
    # Apply context and notification wrappers similar to other tools
    context_wrapped_ws = with_user_context(web_search)
    notification_wrapped_ws = with_completion_notification(context_wrapped_ws)
    if tool_capture:
        final_ws_func = wrap_func_with_capture(notification_wrapped_ws, "web_search")
    else:
        final_ws_func = notification_wrapped_ws

    tools.append(StructuredTool.from_function(
        func=final_ws_func,
        name="web_search",
        description=(
            "Search the web for up-to-date cloud provider documentation, troubleshooting guides, "
            "breaking changes and best practices. Use when you need information that may have "
            "changed after the model's training cutoff or when you require verified external "
            "sources."
        ),
        args_schema=WebSearchArgs,
    ))
    
    tools = ModeAccessController.filter_tools(mode, tools)
    
    # Deduplicate tools by name to prevent "Tool names must be unique" errors with Claude
    seen_tool_names = set()
    deduplicated_tools = []
    for tool in tools:
        tool_name = getattr(tool, 'name', None)
        if tool_name and tool_name not in seen_tool_names:
            deduplicated_tools.append(tool)
            seen_tool_names.add(tool_name)
        elif tool_name:
            logging.warning(f"Skipping duplicate tool: {tool_name}")
        else:
            deduplicated_tools.append(tool)
    
    if len(tools) != len(deduplicated_tools):
        logging.info(f"Deduplicated {len(tools) - len(deduplicated_tools)} duplicate tools")
    tools = deduplicated_tools
    
    logging.info(f"Total tools available: {len(tools)}")
    
    # Cache the fully processed LangChain tools if we have a user_id
    if user_id:
        _langchain_tools_cache[cache_key] = tools
        _langchain_tools_cache_expiry[cache_key] = time.time() + LANGCHAIN_TOOLS_CACHE_DURATION
        logging.info(
            f"Cached {len(tools)} fully processed LangChain tools for user {user_id} (key: {cache_key})"
        )
    
    return tools 

# MCP cleanup and status logging is handled in mcp_tools.py

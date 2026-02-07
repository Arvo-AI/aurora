import logging
# Configure logging first, before importing any modules - INFO level for cleaner logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# Get module logger
logger = logging.getLogger(__name__)

# Reduce verbosity of specific noisy loggers
logging.getLogger("langchain").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)  
logging.getLogger("weaviate").setLevel(logging.WARNING)
logging.getLogger("redis").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure").setLevel(logging.WARNING)

import json
import time
import uuid
import os
from utils.kubectl.agent_ws_handler import handle_kubectl_agent
import psycopg2
from datetime import datetime
from dotenv import load_dotenv
from typing import List, Optional

# Load environment variables
load_dotenv()

from collections import defaultdict
import asyncio
from langchain_core.messages import AIMessageChunk, HumanMessage, AIMessage
import websockets
import logging

from chat.backend.agent.agent import Agent
from chat.backend.agent.db import PostgreSQLClient
from chat.backend.agent.utils.state import State
from chat.backend.agent.workflow import Workflow
from chat.backend.agent.weaviate_client import WeaviateClient
from chat.backend.agent.utils.llm_context_manager import LLMContextManager
from chat.backend.agent.utils.chat_context_manager import ChatContextManager
from chat.backend.agent.utils.immediate_save_handler import handle_immediate_save
from utils.db.connection_pool import db_pool
from utils.billing.billing_cache import update_api_cost_cache_async, get_cached_api_cost
from utils.billing.billing_utils import get_api_cost
from utils.billing.billing_cache import is_cache_fresh
from utils.terraform.terraform_cleanup import cleanup_terraform_directory
from utils.cloud.infrastructure_confirmation import handle_websocket_confirmation_response
from chat.backend.agent.tools.cloud_tools import register_websocket_connection, set_user_context
from chat.backend.agent.access import ModeAccessController
from utils.text.text_utils import clean_markdown
from utils.internal.api_handler import handle_http_request


class RateLimiter:
    def __init__(self, rate, per):
        self.tokens = defaultdict(lambda: rate)
        self.rate = rate
        self.per = per
        self.last_checked = defaultdict(time.time)

    def is_allowed(self, client_id):
        now = time.time()
        elapsed = now - self.last_checked[client_id]
        self.last_checked[client_id] = now

        self.tokens[client_id] += elapsed * (self.rate / self.per)
        if self.tokens[client_id] > self.rate:
            self.tokens[client_id] = self.rate

        if self.tokens[client_id] >= 1:
            self.tokens[client_id] -= 1
            return True
        return False
    
class _NoopAsyncContextManager:
    async def __aenter__(self):
        return None
    async def __aexit__(self, exc_type, exc, tb):
        return False

rate_limiter = RateLimiter(rate=5, per=60)


def _normalize_mode(mode: Optional[str]) -> str:
    return (mode or "agent").strip().lower()

# Note: Removed per-user concurrency limitation to enable true multi-session support
# Concurrent sessions are now safely isolated via session-specific terraform directories

# Deployment update listener removed

async def handle_init(data, websocket, current_user_id, deployment_listener_task):
    """Handle connection initialization message and return updated state."""

    user_id = data.get('user_id')
    if user_id and current_user_id != user_id:
        logger.info(f"Initializing connection for user {user_id}")
        current_user_id = user_id

        # Preemptively warm the API cost cache for this user
        asyncio.create_task(update_api_cost_cache_async(user_id))
        logger.info(f"Started preemptive API cost cache update for user {user_id}")

        # Trigger MCP preloading for this user
        try:
            from chat.backend.agent.tools.mcp_preloader import preload_user_tools
            preload_user_tools(user_id)
            logger.info(f"Triggered MCP preload for user {user_id} on connection")
        except Exception as e:
            logger.debug(f"Failed to trigger MCP preload on connection: {e}")
        
        # Start deployment listener for this user
        if deployment_listener_task:
            deployment_listener_task.cancel()
            try:
                await deployment_listener_task
            except asyncio.CancelledError:
                pass

        # Deployment listener removed
        logger.info(f"Started deployment listener for user {user_id}")

        # Update user activity for MCP preloader
        try:
            from chat.backend.agent.tools.mcp_preloader import update_user_activity
            update_user_activity(user_id)
            logger.debug(f"Updated MCP preloader activity for user {user_id}")
        except Exception as e:
            logger.debug(f"Failed to update MCP preloader activity: {e}")

    return current_user_id, deployment_listener_task
def process_attachments_and_append_to_question(question, attachments):
    """Process incoming attachments, log details, and append a human-readable summary to the question."""
    if attachments:
        logger.info(f"Received {len(attachments)} file attachment(s)")
        # Log attachment details for debugging
        for i, attachment in enumerate(attachments):
            filename = attachment.get('filename', 'unknown')
            file_type = attachment.get('file_type', '')
            is_server_path = attachment.get('is_server_path', False)
            server_path = attachment.get('server_path', '')
            has_file_data = bool(attachment.get('file_data', ''))
            logger.info(f"  Attachment {i}: {filename} (type: {file_type}, server_path: {is_server_path}, path: {server_path}, has_data: {has_file_data})")

        # Process attachments and append to question
        attachment_context = []
        for attachment in attachments:
            filename = attachment.get('filename', 'unknown')
            file_type = attachment.get('file_type', '')
            file_data = attachment.get('file_data', '')
            server_path = attachment.get('server_path', '')
            is_server_path = attachment.get('is_server_path', False)

            if is_server_path and server_path:
                # Handle server-side file paths (e.g., uploaded zip files)
                if file_type in ['application/zip', 'application/x-zip-compressed']:
                    attachment_context.append(f"[Attached ZIP file: {filename} (server path: {server_path})]")
                    logger.info(f"Zip file attachment with server path: {server_path}")
                else:
                    attachment_context.append(f"[Attached file: {filename} (server path: {server_path})]")
            elif file_type.startswith('image/'):
                attachment_context.append(f"[Attached image: {filename}]")
            elif file_type == 'application/pdf':
                attachment_context.append(f"[Attached PDF: {filename}]")

        if attachment_context:
            # Append attachment info to the question
            question = question + "\n\n" + "\n".join(attachment_context)
    else:
        logger.info("No file attachments received in this message")

    return question

def create_websocket_sender(websocket, user_id, session_id):
    """Create an async sender callable for tool outputs over the websocket.
    Keeps original single-argument signature expected by tools and the Agent.
    Gracefully handles WebSocket disconnections without stopping workflow.
    """
    
    async def sender(data):
        try:
            # Handle both string and dictionary inputs
            if isinstance(data, str):
                # If it's a string, try to parse as JSON first
                try:
                    await websocket.send(data)  # Send the original string
                except json.JSONDecodeError:
                    # If not JSON, treat as plain text message
                    message_data = {
                        "type": "message",
                        "data": {"text": data}
                    }
                    await websocket.send(json.dumps(message_data))
            else:
                # Original dictionary handling
                await websocket.send(json.dumps(data))
                try:
                    msg_type = data.get('type', 'unknown')
                except Exception:
                    msg_type = 'unknown'
        except websockets.exceptions.ConnectionClosed:
            logger.debug(f"WEBSOCKET: Connection {id(websocket)} closed, continuing workflow in background")
            return False  # Indicate connection lost but don't stop workflow
        except Exception as e:
            logger.warning(f"WEBSOCKET: Error sending tool data via connection {id(websocket)}: {e}")
            return False
        return True

    # Register this connection with the full sender function
    register_websocket_connection(user_id, session_id, sender, asyncio.get_event_loop(), id(websocket))
    logger.info(f"WEBSOCKET: Registered connection {id(websocket)} for user {user_id}, session {session_id}")

    return sender


async def process_workflow_async(wf, state, websocket, user_id, incident_id=None):
    curr_node = "START"
    sent_message_count = 0
    websocket_connected = True
    workflow_timeout = 1800  # 30 minutes max for any workflow
    session_id = getattr(state, 'session_id', 'unknown')

    # Helper to save incident thoughts for background chats
    accumulated_thought = []
    last_save_time = [time.time()]  # Use list to allow mutation in nested function
    
    def save_incident_thought(content: str, force: bool = False):
        """Save thought to incident_thoughts if this is a background chat with incident_id."""
        if not incident_id:
            return
        
        if content:
            accumulated_thought.append(content)
        
        accumulated_text = "".join(accumulated_thought)
        current_time = time.time()
        time_since_last_save = current_time - last_save_time[0]
        
        # Check if we should try to save
        # Save immediately after sentence boundaries OR every 1 second OR when we have 50+ chars, OR when forced
        # This ensures thoughts stream progressively instead of batching
        should_check = force or time_since_last_save >= 1 or len(accumulated_text) >= 50
        
        if should_check and len(accumulated_text) > 20:  # Lower threshold for more frequent saves
            import re
            # Find sentence boundaries (. ! ? followed by space or end)
            sentence_pattern = r'[.!?](?:\s|$)'
            matches = list(re.finditer(sentence_pattern, accumulated_text))
            
            # Save if: forced, found sentences (save after each sentence)
            if matches or force:
                if force and not matches:
                    # Save everything when forced or text is very long
                    text_to_save = accumulated_text
                    remaining = ""
                else:
                    # Save up to last complete sentence - this makes it save incrementally
                    last_match = matches[-1]
                    split_pos = last_match.end()
                    text_to_save = accumulated_text[:split_pos].strip()
                    remaining = accumulated_text[split_pos:].strip()
                
                if len(text_to_save) > 20:  # Lower minimum to save smaller chunks
                    try:
                        from utils.db.connection_pool import db_pool
                        from datetime import datetime
                        cleaned_text = clean_markdown(text_to_save)
                        
                        # Only save if cleaned text has substantial content
                        if len(cleaned_text) > 20:  # Lower threshold
                            with db_pool.get_admin_connection() as conn:
                                with conn.cursor() as cursor:
                                    cursor.execute(
                                        "INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES (%s, %s, %s, %s)",
                                        (incident_id, datetime.now(), cleaned_text, "analysis")
                                    )
                                conn.commit()
                                logger.info(f"[BackgroundChat] Saved thought for incident {incident_id}: {len(cleaned_text)} chars (incremental save)")
                                accumulated_thought.clear()
                                if remaining:
                                    accumulated_thought.append(remaining)
                                last_save_time[0] = current_time
                    except Exception as e:
                        logger.error(f"[BackgroundChat] Failed to save thought: {e}")
    
    # Helper function to send messages via the appropriate sender
    async def send_via_appropriate_sender(message_data):
        nonlocal websocket_connected
        try:
            # Get the Agent's websocket_sender fresh each time (it may have been updated)
            current_agent_sender = None
            if hasattr(wf, 'agent') and hasattr(wf.agent, 'websocket_sender'):
                current_agent_sender = wf.agent.websocket_sender
            
            if current_agent_sender:
                # Use Agent's websocket_sender (which may have been updated)
                success = await current_agent_sender(message_data)
                if not success:
                    websocket_connected = False
                    logger.warning("WEBSOCKET: Agent websocket_sender failed - connection may be closed")
            else:
                # Fallback to original websocket
                await websocket.send(json.dumps(message_data))
        except websockets.exceptions.ConnectionClosed:
            websocket_connected = False
            logger.warning("WEBSOCKET: Connection closed during send - continuing workflow in background")
        except Exception as e:
            logger.warning(f"WEBSOCKET: Error sending message: {e}")
            websocket_connected = False
    
    # Helper to send END status - called on completion or timeout
    async def send_end_status(reason="completed"):
        if websocket_connected:
            final_response = {
                "type": "status",
                "data": {"status": "END"},
                "isComplete": True,
            }
            if hasattr(state, 'session_id') and state.session_id:
                final_response["session_id"] = state.session_id
            try:
                await send_via_appropriate_sender(final_response)
                logger.info(f"Sent END status for session {session_id} (reason: {reason})")
            except Exception as e:
                logger.error(f"Failed to send END status for session {session_id}: {e}")
    
    try:
        # Wrap the workflow stream with timeout to prevent infinite hangs
        async def process_stream():
            nonlocal sent_message_count
            # Track tool calls we've already sent to avoid duplicates
            sent_tool_call_ids = set()
            event_count = 0
            
            try:
                async for event_type, event_data in wf.stream(state):
                    event_count += 1
                    
                    if event_type == "token":
                        # Real-time token streaming from LLM
                        if websocket_connected:
                            token_text = event_data  # event_data is the token string
                            if token_text:
                                msg_response = {
                                    "type": "message",
                                    "data": {
                                        "text": token_text,
                                        "is_chunk": True,
                                        "is_complete": False,
                                        "streaming": True
                                    },
                                }
                                if hasattr(state, 'session_id') and state.session_id:
                                    msg_response["session_id"] = state.session_id
                                
                                await send_via_appropriate_sender(msg_response)
                                # Save tokens incrementally to incident thoughts
                                save_incident_thought(token_text, force=False)
                    
                    elif event_type == "values":
                        # Final state update
                        logger.debug(f"[STREAM DEBUG] Received values event")
                        # Handle final state if needed
                        continue
                    
                    elif event_type == "messages":
                        try:
                            msg_chunk, _ = event_data
                            logger.debug(f"[STREAM DEBUG] Successfully unpacked message chunk: type={type(msg_chunk).__name__}, content_length={len(str(getattr(msg_chunk, 'content', '')))}, is AIMessageChunk={isinstance(msg_chunk, AIMessageChunk)}")
                        except (ValueError, TypeError) as unpack_error:
                            logger.error(f"[STREAM ERROR] Failed to unpack message event_data: {unpack_error}, event_data type: {type(event_data)}, event_data: {event_data}")
                            continue
                        
                        logger.info(f"[STREAM DEBUG] Message chunk type: {type(msg_chunk).__name__}, is AIMessageChunk: {isinstance(msg_chunk, AIMessageChunk)}, is AIMessage: {isinstance(msg_chunk, AIMessage)}, websocket_connected: {websocket_connected}, has_content: {bool(getattr(msg_chunk, 'content', None))}")
                        
                        # Handle both AIMessageChunk (streaming) and AIMessage (complete) from langchain
                        if isinstance(msg_chunk, (AIMessageChunk, AIMessage)) and websocket_connected:
                            # Check for tool calls in the message chunk and send them
                            tool_calls = None
                            if hasattr(msg_chunk, 'tool_calls') and msg_chunk.tool_calls:
                                tool_calls = msg_chunk.tool_calls
                            elif hasattr(msg_chunk, 'additional_kwargs') and msg_chunk.additional_kwargs:
                                tool_calls = msg_chunk.additional_kwargs.get('tool_calls', [])
                            
                            # Send tool calls that haven't been sent yet
                            if tool_calls:
                                for tool_call in tool_calls:
                                    tool_call_id = tool_call.get('id') or tool_call.get('tool_call_id')
                                    if tool_call_id and tool_call_id not in sent_tool_call_ids:
                                        sent_tool_call_ids.add(tool_call_id)
                                        tool_name = tool_call.get('function', {}).get('name', 'unknown')
                                        tool_args = tool_call.get('function', {}).get('arguments', '{}')
                                        
                                        try:
                                            tool_input = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                        except:
                                            tool_input = tool_args
                                        
                                        tool_call_msg = {
                                            "type": "tool_call",
                                            "data": {
                                                "tool_name": tool_name,
                                                "input": tool_input,
                                                "status": "running",
                                                "timestamp": datetime.now().isoformat(),
                                                "tool_call_id": tool_call_id
                                            }
                                        }
                                        if hasattr(state, 'session_id') and state.session_id:
                                            tool_call_msg["session_id"] = state.session_id
                                        await send_via_appropriate_sender(tool_call_msg)
                                        logger.debug(f"Sent tool call via streaming: {tool_name} (id: {tool_call_id})")
                            
                            # Send message content if present
                            chunk_content = getattr(msg_chunk, 'content', '')
                            logger.info(f"[STREAM DEBUG] Checking content: chunk_content length={len(str(chunk_content))}, empty={chunk_content == ''}, type={type(chunk_content)}")
                            if chunk_content and str(chunk_content).strip() != "":
                                # Split large chunks into smaller pieces for smoother streaming
                                # This ensures thoughts appear progressively even when LLM sends large chunks
                                import re
                                raw_content = msg_chunk.content
                                cleaned_content = re.sub(r'\s{3,}', ' ', raw_content)  # Replace 3+ consecutive whitespace with single space
                                cleaned_content = re.sub(r'\n{3,}', '\n\n', cleaned_content)  # Replace 3+ newlines with double newline
                                
                                if cleaned_content:  # Only send non-empty chunks after cleaning
                                    # Split large chunks into sentences for smoother streaming
                                    sentences = re.split(r'([.!?]\s+)', cleaned_content)
                                    current_chunk = ""
                                    
                                    for i, part in enumerate(sentences):
                                        current_chunk += part
                                        # Send when we hit sentence boundary or chunk is getting large
                                        is_sentence_boundary = bool(re.match(r'[.!?]\s+$', part))
                                        if is_sentence_boundary or len(current_chunk) > 100:
                                            if current_chunk.strip():
                                                msg_response = {
                                                    "type": "message",
                                                    "data": {
                                                        "text": current_chunk,
                                                        "is_chunk": True,
                                                        "is_complete": False,
                                                        "streaming": True
                                                    },
                                                }
                                                if hasattr(state, 'session_id') and state.session_id:
                                                    msg_response["session_id"] = state.session_id
                                                logger.debug(f"[STREAM SEND] Sending split chunk ({len(current_chunk)} chars)")
                                                await send_via_appropriate_sender(msg_response)
                                                # Save to incident thoughts incrementally
                                                save_incident_thought(current_chunk, force=False)
                                                current_chunk = ""
                                    
                                    # Send any remaining content
                                    if current_chunk.strip():
                                        msg_response = {
                                            "type": "message",
                                            "data": {
                                                "text": current_chunk,
                                                "is_chunk": True,
                                                "is_complete": False,
                                                "streaming": True
                                            },
                                        }
                                        if hasattr(state, 'session_id') and state.session_id:
                                            msg_response["session_id"] = state.session_id
                                        logger.debug(f"[STREAM SEND] Sending final split chunk ({len(current_chunk)} chars)")
                                        await send_via_appropriate_sender(msg_response)
                                        save_incident_thought(current_chunk, force=False)
                    elif event_type == "values":
                        # Handle "values" events - complete messages from state updates
                        if hasattr(event_data, 'messages') and event_data.messages and websocket_connected:
                            current_message_count = len(event_data.messages)
                            if current_message_count > sent_message_count:
                                for i in range(sent_message_count, current_message_count):
                                    message = event_data.messages[i]
                                    if isinstance(message, AIMessage):
                                        # Send tool calls if present
                                        tool_calls = None
                                        if hasattr(message, 'tool_calls') and message.tool_calls:
                                            tool_calls = message.tool_calls
                                        elif hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                                            tool_calls = message.additional_kwargs.get('tool_calls', [])
                                        
                                        if tool_calls:
                                            for tool_call in tool_calls:
                                                tool_call_id = tool_call.get('id') or tool_call.get('tool_call_id')
                                                if tool_call_id and tool_call_id not in sent_tool_call_ids:
                                                    sent_tool_call_ids.add(tool_call_id)
                                                    tool_name = tool_call.get('function', {}).get('name') or tool_call.get('name', 'unknown')
                                                    tool_args = tool_call.get('function', {}).get('arguments') or tool_call.get('args', '{}')
                                                    
                                                    try:
                                                        tool_input = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                                    except:
                                                        tool_input = tool_args
                                                    
                                                    tool_call_msg = {
                                                        "type": "tool_call",
                                                        "data": {
                                                            "tool_name": tool_name,
                                                            "input": tool_input,
                                                            "status": "running",
                                                            "timestamp": datetime.now().isoformat(),
                                                            "tool_call_id": tool_call_id
                                                        }
                                                    }
                                                    if hasattr(state, 'session_id') and state.session_id:
                                                        tool_call_msg["session_id"] = state.session_id
                                                    await send_via_appropriate_sender(tool_call_msg)
                                                    logger.debug(f"Sent tool call from values event: {tool_name} (id: {tool_call_id})")
                                        
                                        # Send message content if present
                                        if hasattr(message, 'content') and message.content:
                                            complete_msg_response = {
                                                "type": "message",
                                                "data": {"text": message.content},
                                            }
                                            if hasattr(state, 'session_id') and state.session_id:
                                                complete_msg_response["session_id"] = state.session_id
                                            await send_via_appropriate_sender(complete_msg_response)
                                            # Force save accumulated thought before starting new message
                                            save_incident_thought("", force=True)
                                            save_incident_thought(message.content, force=False)
                                sent_message_count = current_message_count
                    else:
                        logger.warning(f"[STREAM DEBUG] Unhandled event type: {event_type}, data type: {type(event_data).__name__}")
                
                logger.info(f"[STREAM DEBUG] Stream completed. Total events: {event_count}")
            except Exception as stream_error:
                logger.error(f"[STREAM ERROR] Error in process_stream for session {session_id}: {stream_error}", exc_info=True)
                raise
        
        # Execute workflow with timeout
        await asyncio.wait_for(process_stream(), timeout=workflow_timeout)
        
        # CRITICAL: Wait for any ongoing tool calls to complete before marking as done
        if hasattr(wf, '_wait_for_ongoing_tool_calls'):
            await wf._wait_for_ongoing_tool_calls()
        logger.info(f"Workflow completed normally for session {session_id} - WebSocket connected: {websocket_connected}")
        
        # Force save any remaining accumulated thought
        save_incident_thought("", force=True)
        
        await send_end_status("completed")
        
    except asyncio.TimeoutError:
        logger.error(f"Workflow timeout after {workflow_timeout}s for session {session_id}")
        if websocket_connected:
            timeout_msg = {
                "type": "message",
                "data": {"text": "\n\n️ Workflow timeout - the operation may have completed but the response took too long. Please check your resources manually."},
            }
            if hasattr(state, 'session_id') and state.session_id:
                timeout_msg["session_id"] = state.session_id
            await send_via_appropriate_sender(timeout_msg)
        await send_end_status("timeout")
        
    except Exception as e:
        logger.error(f"Error in workflow processing for session {session_id}: {e}", exc_info=True)
        if websocket_connected:
            error_msg = {
                "type": "message",
                "data": {"text": f"\n\nWorkflow error: {str(e)}"},
            }
            if hasattr(state, 'session_id') and state.session_id:
                error_msg["session_id"] = state.session_id
            await send_via_appropriate_sender(error_msg)
        await send_end_status("error")

    # Handle API cost tracking - always execute regardless of WebSocket status
    if user_id:
        try:
            # Update API cost cache after workflow completion to capture new usage
            # This ensures that any LLM usage from this workflow is immediately reflected
            asyncio.create_task(update_api_cost_cache_async(user_id))
            logger.debug(f"Triggered post-request API cost update for user {user_id}")

            is_cached, total_cost = get_cached_api_cost(user_id)

            if not is_cached:
                total_cost = get_api_cost(user_id)
                logger.info(f"API cost tracking for user {user_id}: ${total_cost:.2f}")

            # Log API usage information (for cost tracking purposes)
            logger.info(f"User {user_id} API cost: ${total_cost:.2f} (WebSocket connected: {websocket_connected})")

            # Send usage info if WebSocket is connected (for cost tracking display)
            if websocket_connected:
                usage_info_response = {
                    "type": "usage_info",
                    "data": {
                        "total_cost": round(total_cost, 2),
                    },
                }
                await send_via_appropriate_sender(usage_info_response)
        except Exception as e:
            logger.error(f"Error tracking API costs: {e}")
    
    # Workflow always completes here - messages are saved by the workflow.stream() method
    logger.info(f"Background workflow processing completed for session {getattr(state, 'session_id', 'unknown')}")

async def handle_connection(websocket) -> None:
    """Handle a single WebSocket connection."""
    auth_header = websocket.request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        await handle_kubectl_agent(websocket)
        return
    
    client_id = id(websocket)
    logger.info(f"New client connected. ID: {client_id}")

    weaviate_client = None
    deployment_listener_task = None
    current_user_id = None
    session_id = None       # Initialize to avoid UnboundLocalError in exception handler
    session_tasks = {}      # Track running workflow asyncio.Tasks per session

    
    try:
        postgres_client = PostgreSQLClient()
        weaviate_client = WeaviateClient(postgres_client)
        # Note: Agent will be created with websocket_sender in the message processing loop
        agent = None
        wf = None 
    except Exception as e: 
        # Log the error and send an error message to the client
        logger.error("Workflow Initialization Error: %s", e, exc_info=True)
        await websocket.send(json.dumps({
            "type": "error",
            "data": {
                "text": f"Unexpected error: {str(e)}",
                "session_id": session_id,
            }
        }))
        # Close the connection to weaviate if opened
        logger.debug(f"weaviate client is {weaviate_client}")
        if weaviate_client:
            weaviate_client.close()
        return

    # Send ready status to client
    await websocket.send(json.dumps({
        "type": "status",
        "data": {
            "status": "START",
        },
    }))

    # Listen for incoming messages
    try:
        # Main message loop. Will run each time a message is received from the frontend (aka sent by the user)
        async for message in websocket:
            # Rate limit check
            if not rate_limiter.is_allowed(client_id):
                logger.warning(f"Rate limit exceeded for client {client_id}")
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {"text": f"Rate limit exceeded. Please wait and try again."},
                }))
                continue

            logger.debug(f"Received message from client {client_id}: {message}")
            data = json.loads(message)

            # Handle connection initialization for the websocket
            if data.get('type') == 'init':
                current_user_id, deployment_listener_task = await handle_init(
                    data, websocket, current_user_id, deployment_listener_task
                )
                continue
            
            # Handle confirmation responses - WebSocket Connection Refresh Feature
            # When a user reconnects and sends a confirmation response, we update the workflow's
            # WebSocket connection to use the new connection instead of the old closed one.
            if data.get('type') == 'confirmation_response':
                logger.info(f"WEBSOCKET: Received confirmation response via connection {client_id}: {data.get('confirmation_id')}")

                # Extract user_id and session_id from confirmation response
                response_user_id = data.get('user_id')
                response_session_id = data.get('session_id')

                # Register this connection if we have user_id and session_id
                if response_user_id and response_session_id:
                    from chat.backend.agent.tools.cloud_tools import register_websocket_connection
                    # Create a simple sender function for this connection
                    async def confirmation_sender(data):
                        try:
                            await websocket.send(json.dumps(data))
                            return True
                        except Exception as e:
                            logger.warning(f"WEBSOCKET: Error sending confirmation response: {e}")
                            return False

                    register_websocket_connection(response_user_id, response_session_id, confirmation_sender, asyncio.get_event_loop(), client_id)
                    logger.info(f"WEBSOCKET: Registered connection {client_id} for confirmation response (user: {response_user_id}, session: {response_session_id})")

                    # Create a new websocket_sender for the Agent to use the new connection
                    if agent and hasattr(agent, 'update_websocket_sender'):
                        new_websocket_sender = create_websocket_sender(websocket, response_user_id, response_session_id)
                        agent.update_websocket_sender(new_websocket_sender, asyncio.get_event_loop())

                await handle_websocket_confirmation_response(data)

                continue
            
            # Handle control messages (like cancel)
            if data.get('type') == 'control':
                if data.get('action') == 'cancel':
                    logger.debug(f"Cancel request received from client {client_id}")
                    # Extract identifiers carried by the control message (defensive – may be absent)
                    session_id = data.get('session_id')
                    user_id = data.get('user_id')

                    # Cancel any pending confirmations to unblock waiting threads
                    from utils.cloud.infrastructure_confirmation import cancel_pending_confirmations_for_session
                    cancelled_count = cancel_pending_confirmations_for_session(session_id)
                    if cancelled_count > 0:
                        logger.info(f"Cancelled {cancelled_count} pending confirmation(s) for session {session_id}")

                    # Retrieve the running task for this session
                    running = session_tasks.get(session_id)

                    logger.debug(f"Attempting to cancel running workflow task for session {session_id}")

                    if running and not running.done():
                        # Ask the task to cancel **first** so no more chunks are appended
                        running.cancel()

                        try:
                            # Await the task so cancellation propagates cleanly
                            await running
                        except asyncio.CancelledError:
                            logger.info(f"Workflow task for session {session_id} acknowledged cancellation")

                        # Consolidate any remaining message chunks into full messages
                        if wf:
                            # Wait for any tool calls to complete before consolidating
                            await wf._wait_for_ongoing_tool_calls()

                            # Collect any remaining tool messages before consolidation
                            # This adds them to the state so they are not lost
                            wf._collect_remaining_tool_messages()

                            wf._consolidate_message_chunks()

                            # Persist the consolidated context so the chat can be resumed later
                            if wf._last_state:
                                messages = (
                                    wf._last_state.get('messages', [])
                                    if hasattr(wf._last_state, 'get')
                                    else getattr(wf._last_state, 'messages', [])
                                )

                                if messages and session_id and user_id:
                                    # Add a strong cancellation message directly to the context
                                    # Using message with strong formatting to create a powerful interrupt signal
                                    # This is marked as a human message so it appears as if the user is cancelling the request. It is not saved to the UI (messages) field only in the context (llm_context_history) field.
                                    interrupt_message = HumanMessage(
                                        content="[URGENT CANCELLATION] The previous request has been cancelled. **CRITICAL INSTRUCTION:** You MUST abandon the previous plan entirely. Acknowledge the cancellation internally and respond to the user's very latest message."
                                    )
                                    messages.append(interrupt_message)

                                    tool_capture = getattr(wf.agent, 'tool_capture_instance', None)
                                    saved = LLMContextManager.save_context_history(
                                        session_id,
                                        user_id,
                                        messages,
                                        tool_capture,
                                    )
                                    if saved:
                                        logger.info(
                                            f"Successfully saved context after cancellation for session {session_id} ({len(messages)} messages, including cancellation notice)"
                                        )
                                    else:
                                        logger.warning(
                                            f"Failed to save context after cancellation for session {session_id}"
                                        )
                                    
                                    # ALSO save UI messages after cancellation
                                    ui_messages = wf._convert_to_ui_messages(messages, tool_capture)
                                    ui_saved = wf._save_ui_messages(session_id, user_id, ui_messages, wf._ui_state)
                                    if ui_saved:
                                        logger.info(f"Successfully saved UI messages after cancellation for session {session_id}")
                                    else:
                                        logger.warning(f"Failed to save UI messages after cancellation for session {session_id}")
                
                # Send END status to frontend after cancellation cleanup
                try:
                    end_response = {
                        "type": "status",
                        "data": {"status": "END"},
                        "isComplete": True,
                        "session_id": session_id
                    }
                    await websocket.send(json.dumps(end_response))
                    logger.info(f"Sent END status to frontend after cancellation for session {session_id}")
                except Exception as e:
                    logger.warning(f"Failed to send END status after cancellation for session {session_id}: {e}")

                # Nothing more to do for this control message – return to listening loop
                continue
            
            # Extract the question from the incoming data
            question = data.get('query')
            
            logger.info(f"Processing question: {question}")
            
            user_id = data.get('user_id')  # Extract user_id from the incoming data
            session_id = data.get('session_id')  # Extract session_id from the incoming data
            
            # Get connected providers from database instead of relying on frontend preferences
            from utils.auth.stateless_auth import get_connected_providers
            if user_id:
                provider_preference = get_connected_providers(user_id)
            else:
                provider_preference = None

            # Extract direct_tool_call which is sent at the top level of the message
            direct_tool_call = data.get('direct_tool_call')
            # Handle both old single provider format and new multiple provider format
            if isinstance(provider_preference, str):
                provider_preference = [provider_preference]
            if isinstance(provider_preference, list):
                # Build valid_providers list, conditionally including ovh
                from utils.flags.feature_flags import is_ovh_enabled
                valid_providers = ['gcp', 'azure', 'aws', 'scaleway', 'tailscale']
                if is_ovh_enabled():
                    valid_providers.append('ovh')
                provider_preference = [p for p in provider_preference if p in valid_providers]
                if not provider_preference:
                    provider_preference = None
            else:
                provider_preference = None
            selected_project_id = data.get('selected_project_id')  # Extract selected project ID if provided
            model = data.get('model')  # Extract selected model from frontend
            mode_input = data.get('mode')    # Extract chat mode (agent / ask)
            attachments = data.get('attachments', []) # Extract file attachments if present

            mode = _normalize_mode(mode_input)

            # Extract UI state to save with the session
            ui_state = data.get('ui_state', {
                'selectedModel': model,
                'selectedMode': mode,
                'selectedProviders': provider_preference or []
            })
            if not user_id:
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {"text": "Missing user_id in the message."}
                }))
                continue
            logger.info(f"Processing question from user {user_id}: {question}")
            logger.info(f"Using chat session: {session_id} and provider_preference: {provider_preference}")
            
            # Check if this is a direct tool call that should bypass the AI
            if direct_tool_call:
                logger.info(f"Direct tool call requested: {direct_tool_call}")
                tool_name = direct_tool_call.get('tool_name')
                parameters = direct_tool_call.get('parameters', {})
                
                # Handle github_commit tool directly
                if tool_name == 'github_commit':
                    logger.info(f"Executing direct github_commit tool call with params: {parameters}")
                    if not ModeAccessController.is_tool_allowed(mode, tool_name):
                        warning_msg = "github_commit is not available in Ask mode. Switch to Agent mode to push changes."
                        await websocket.send(json.dumps({
                            "type": "error",
                            "data": {
                                "text": warning_msg,
                                "session_id": session_id,
                                "code": "READ_ONLY_MODE"
                            }
                        }))
                        continue
                    
                    # Import and execute the tool
                    try:
                        from chat.backend.agent.tools.github_commit_tool import github_commit
                        # set_user_context already imported at top of file
                        
                        # Set user context for the tool
                        class MockState:
                            def __init__(self, session_id):
                                self.session_id = session_id
                        
                        mock_state = MockState(session_id)
                        set_user_context(
                            user_id=user_id,
                            session_id=session_id,
                            provider_preference=provider_preference,
                            selected_project_id=selected_project_id,
                            state=mock_state,
                            mode=mode,
                        )
                        
                        # Execute the tool
                        result = github_commit(
                            repo=parameters.get('repo'),
                            commit_message=parameters.get('commit_message'),
                            branch=parameters.get('branch', 'main'),
                            push=parameters.get('push', True),
                            user_id=user_id,
                            session_id=session_id
                        )
                        
                        # Send the result back
                        await websocket.send(json.dumps({
                            "type": "tool_result",
                            "data": {
                                "tool_name": tool_name,
                                "result": result,
                                "session_id": session_id
                            }
                        }))
                        
                        logger.info(f"Direct tool call completed: {tool_name}")
                        continue  # Skip normal AI processing
                        
                    except Exception as e:
                        logger.error(f"Error executing direct tool call {tool_name}: {e}")
                        await websocket.send(json.dumps({
                            "type": "error",
                            "data": {
                                "text": f"Failed to execute {tool_name}: {str(e)}",
                                "session_id": session_id
                            }
                        }))
                        continue

                # Handle IaC tool directly from the frontend (unified entry point)
                elif tool_name == 'iac_tool':
                    logger.info(f"Executing direct iac_tool call with params: {parameters}")

                    try:
                        # set_user_context already imported at top of file
                        from chat.backend.agent.tools.iac_tool import run_iac_tool

                        class MockState:
                            def __init__(self, session_id):
                                self.session_id = session_id

                        mock_state = MockState(session_id)
                        set_user_context(
                            user_id=user_id,
                            session_id=session_id,
                            provider_preference=provider_preference,
                            selected_project_id=selected_project_id,
                            state=mock_state,
                            mode=mode,
                        )

                        # Derive the requested action (maintain backward compatibility for legacy tool names)
                        action = parameters.get('action', 'write')
                        is_allowed_action, denial_message = ModeAccessController.ensure_iac_action_allowed(mode, action)
                        if not is_allowed_action:
                            await websocket.send(json.dumps({
                                "type": "error",
                                "data": {
                                    "text": denial_message,
                                    "session_id": session_id,
                                    "code": "READ_ONLY_MODE"
                                }
                            }))
                            continue

                        result_payload = run_iac_tool(
                            action=action,
                            path=parameters.get('path'),
                            content=parameters.get('content'),
                            directory=parameters.get('directory'),
                            vars=parameters.get('vars'),
                            auto_approve=parameters.get('auto_approve'),
                            user_id=user_id,
                            session_id=session_id
                        )

                        await websocket.send(json.dumps({
                            "type": "tool_result",
                            "data": {
                                "tool_name": 'iac_tool',
                                "result": result_payload,
                                "session_id": session_id
                            }
                        }))

                        logger.info(f"Direct tool call completed: {tool_name}")
                        continue

                    except Exception as e:
                        logger.error(f"Error executing direct tool call {tool_name}: {e}")
                        await websocket.send(json.dumps({
                            "type": "error",
                            "data": {
                                "text": f"Failed to execute {tool_name}: {str(e)}",
                                "session_id": session_id
                            }
                        }))
                        continue


            # Start deployment listener if user_id changed
            if current_user_id != user_id:
                # Cancel existing deployment listener
                if deployment_listener_task:
                    deployment_listener_task.cancel()
                    try:
                        await deployment_listener_task
                    except asyncio.CancelledError:
                        pass
                
                # Start new deployment listener for this user
                current_user_id = user_id
                # Deployment listener removed

            # Set the session variable for RLS enforcement
            try:
                postgres_client.set_user_context(user_id)
            except Exception as e:
                logger.error("Failed to set user context: %s", e, exc_info=True)
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {"text": "Internal error setting user context.", "session_id": session_id}
                }))
                continue


            # EARLY VALIDATION: Check token count BEFORE creating any messages, states, or workflows
            # This prevents large messages from entering the system at all
            try:
                input_token_count = ChatContextManager.count_tokens_in_messages([HumanMessage(content=question)], "gpt-4")
                logger.info(f"User input token count: {input_token_count}")
                
                if input_token_count > 20000:
                    logger.warning(f"User input exceeds 20k token limit: {input_token_count} tokens")
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {
                            "text": f"Your message is too long ({input_token_count} tokens). Please limit your message to 20,000 tokens (approximately 80,000 characters).",
                            "severity": "error",
                            "session_id": session_id,
                        }
                    }))
                    continue  # Skip processing this message
                    
            except Exception as e:
                logger.error(f"Error counting input tokens: {e}")
                # Block processing if token counting fails for safety
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {
                        "text": "Unable to process message due to token counting error. Please try again or contact support if this persists.",
                        "severity": "error",
                        "session_id": session_id,
                    }
                }))
                continue  # Skip processing this message when token counting fails

            # Create websocket sender function for tool calls
            websocket_sender = create_websocket_sender(websocket, user_id, session_id)
            
            # Set user context with session_id for terminal pod isolation
            set_user_context(
                user_id=user_id,
                session_id=session_id,
                provider_preference=provider_preference,
                selected_project_id=selected_project_id,
                mode=mode,
            )
            logger.info(f"Set user context with session_id {session_id} for terminal pod isolation")

            # Create agent and workflow with websocket sender if not already created
            if agent is None:
                agent = Agent(weaviate_client=weaviate_client, postgres_client=postgres_client, websocket_sender=websocket_sender, event_loop=asyncio.get_event_loop())
                logger.info(f"Created agent with websocket sender")
            
            # Create session-specific workflow instance to allow resumption while preventing cross-session leakage
            # Generate a temporary session_id if none provided (for new chats)
            effective_session_id = session_id or f"temp_{user_id}_{uuid.uuid4().hex[:8]}"
            wf = Workflow(agent, effective_session_id)
            logger.info(f"Created new workflow instance with session_id: {effective_session_id}")


            # Create the message content with attachments if present
            if attachments:
                # Create multimodal message content for OpenRouter
                content_parts = [{"type": "text", "text": question}]
                
                for attachment in attachments:
                    file_data = attachment.get('file_data', '')
                    file_type = attachment.get('file_type', '')
                    filename = attachment.get('filename', 'unknown')
                    
                    if file_type.startswith('image/'):
                        # Add image to content parts
                        # Frontend now sends only base64 data, so we need to reconstruct the data URL
                        data_url = f"data:{file_type};base64,{file_data}"
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": data_url
                            }
                        })
                        logger.info(f" Added image to multimodal content: {filename} ({len(file_data)} chars base64)")
                    elif file_type == 'application/pdf':
                        # Add PDF as file content
                        content_parts.append({
                            "type": "file",
                            "file": {
                                "filename": filename,
                                "file_data": file_data  # Already includes data:application/pdf;base64, prefix
                            }
                        })
                
                # For multimodal content, we need to use the content parts format
                human_message = HumanMessage(content=content_parts)
            else:
                # Regular text-only message
                human_message = HumanMessage(content=question)

            # Prepare messages list 
            messages_list = [human_message]

            state = State(
                user_id=user_id,
                session_id=session_id,
                provider_preference=provider_preference,
                selected_project_id=selected_project_id,
                messages=messages_list,
                question=question,
                attachments=attachments,
                model=model,
                mode=mode
            )

            logger.info(f"Created state with {len(attachments) if attachments else 0} attachments for regular query")
            logger.info(f"WebSocket sender initialized: {websocket_sender is not None}")

            # IMMEDIATE SAVE: Save user message immediately when received
            if session_id and user_id:
                handle_immediate_save(session_id, user_id, question, messages_list)

            # Launch workflow processing as async task without blocking
            # Set UI state in workflow before processing so it gets saved
            wf._ui_state = ui_state
            
            task = asyncio.create_task(process_workflow_async(wf, state, websocket, user_id))
            session_tasks[effective_session_id] = task

            continue  # Immediately return to listening for next message

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"WebSocket connection closed gracefully for client {client_id}")
    except Exception as e:
        logger.error("Error processing incoming message: %s", e, exc_info=True)
        try:
            await websocket.send(json.dumps({
                "type": "error",
                "data": {"text": f"Unexpected error: {str(e)}", "session_id": session_id},
            }))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Could not send error message - connection already closed")
    finally:
        logger.info(f"Cleaning up connection for client {client_id}")
        
        # Note: Removed per-user workflow cleanup - users can now run multiple concurrent sessions
        
        # Cancel deployment listener task
        if deployment_listener_task:
            deployment_listener_task.cancel()
            try:
                await deployment_listener_task
            except asyncio.CancelledError:
                pass
        
        if weaviate_client:
            weaviate_client.close()

async def main():
    """Start the WebSocket server and health check endpoint."""
    WS_PORT = 5006
    HEALTH_PORT = 5007
    logger.info("Starting WebSocket server...")

    # Clean up old terraform files on startup
    cleanup_terraform_directory()

    # Start HTTP server (health check + internal API)
    http_server = await asyncio.start_server(handle_http_request, "0.0.0.0", HEALTH_PORT)
    logger.info(f"HTTP server (health + internal API) listening on port {HEALTH_PORT}")

    async with websockets.serve(
        handle_connection,
        host="0.0.0.0",  # Allow external access
        port=WS_PORT,
        ping_interval=20,  # Send ping every 20 seconds
        ping_timeout=10,   # Wait 10 seconds for pong response
        max_size=10 * 1024 * 1024,  # 10MB max message size
        compression=None   # Disable compression to avoid frame issues
    ):
        logger.info(f"WebSocket server listening on port {WS_PORT}")
        await asyncio.Future()

# Terraform cleanup is now handled by utils/terraform_cleanup.py

if __name__ == "__main__":
    try:
        # Start MCP preloader service for faster chat responses
        try:
            from chat.backend.agent.tools.mcp_preloader import start_mcp_preloader
            mcp_preloader = start_mcp_preloader()
            logger.info("MCP Preloader service started successfully")
        except Exception as e:
            logger.warning(f"Failed to start MCP preloader service: {e}")
        
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)

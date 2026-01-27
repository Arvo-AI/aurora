"""
Tool Context Capture with Automatic Summarization

This module provides automatic summarization of tool results to reduce token usage in LLM context
while preserving full output visibility for users.

Key Features:
1. **Automatic Summarization**: Tool outputs exceeding 10,000 tokens are automatically summarized
2. **Dual Message System**: 
   - Frontend receives full output via WebSocket (unchanged user experience)
   - LLM context receives summarized version to save tokens
3. **Internal Messages**: Summarized tool results use InternalToolMessage class with "internal" flag
4. **Fallback Handling**: If summarization fails, content is truncated with error indication
5. **Model Selection**: Uses TOOL_OUTPUT_SUMMARIZATION_MODEL for quick processing of large outputs

Architecture:
- Tool decorators send full output to frontend via WebSocket
- ToolContextCapture intercepts tool results for LLM context  
- Content over 10k tokens triggers summarization via LLMManager.summarize()
- Summarized content marked as "internal" and hidden from UI
- LLM gets concise summaries while users see complete data

Usage:
- Completely automatic and transparent
- No changes needed to existing tools
- Threshold configurable via SUMMARIZATION_THRESHOLD_TOKENS constant
"""

import json
import logging
import threading
import tiktoken
from datetime import datetime
from typing import Dict, Any, List, Optional
from ..llm import LLMManager
# Import langchain components - direct imports for LangChain 1.2.6+
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.callbacks import BaseCallbackHandler

class InternalToolMessage(ToolMessage):
    """A tool message that is for internal LLM consumption only and should not be displayed in the UI."""
    
    def __init__(self, content: str, tool_call_id: str = "", **kwargs):
        super().__init__(content=content, tool_call_id=tool_call_id, **kwargs)
        # Ensure internal flag is in additional_kwargs
        if not hasattr(self, 'additional_kwargs'):
            self.additional_kwargs = {}
        self.additional_kwargs["internal"] = True

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Token counting utility
# --------------------------------------------------------------------------------------
def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """Count tokens in text using tiktoken for the specified model."""
    try:
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))
    except KeyError:
        # Fallback to cl100k_base encoding if model not found
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))

class ToolContextCapture:
    """Captures complete tool interactions for LLM context history."""
    
    def __init__(self, session_id: str, user_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self.current_tool_calls = {}  # Track ongoing tool calls
        self.collected_tool_messages = []  # Store tool messages for batch addition
        self.tool_execution_signatures = set()  # Track unique tool executions to prevent duplicates
        # Map message content to correct tool_call_id (survives LangGraph mutations)
        self.content_to_tool_id = {}  # Map: content_hash → tool_call_id
        # Lock to guard concurrent access to current_tool_calls from concurrent threads/tasks
        self.lock = threading.Lock()
        # Enforce sequential tool execution per session
        self.execution_lock = threading.Lock()
        
    def capture_tool_start(self, tool_name: str, tool_input: Any, tool_call_id: Optional[str] = None) -> str:
        """Capture the start of a tool execution with improved ID management."""    
        
        # Create a normalized signature for this tool execution to detect duplicates
        # Use sorted JSON to ensure consistent signature regardless of dict key order
        import json
        try:
            normalized_input = json.dumps(tool_input, sort_keys=True) if isinstance(tool_input, dict) else str(tool_input)
        except (TypeError, ValueError):
            normalized_input = str(tool_input)
        tool_signature = f"{tool_name}_{normalized_input}"
        
        # Check if we already have a similar tool execution in progress
        # Only reuse if the exact same tool with exact same input is already running
        with self.lock: # Use lock for concurrent access
            for existing_call_id, call_info in self.current_tool_calls.items():
                try:
                    normalized_existing_input = json.dumps(call_info['input'], sort_keys=True) if isinstance(call_info['input'], dict) else str(call_info['input'])
                except (TypeError, ValueError):
                    normalized_existing_input = str(call_info['input'])
                existing_signature = f"{call_info['tool_name']}_{normalized_existing_input}"
                if existing_signature == tool_signature:
                    # Check if the tool call is recent (within 30 seconds) to avoid stale matches
                    time_diff = (datetime.now() - call_info['start_time']).total_seconds()
                    if time_diff < 30:
                        logger.info(f"Detected duplicate tool execution for {tool_name}, reusing existing call_id: {existing_call_id}")
                        return existing_call_id
                    else:
                        logger.warning(f"Found stale tool call for {tool_name} (age: {time_diff}s), creating new one")
        
        logger.info(f"TOOL CAPTURE: Capturing tool start: {tool_name} with ID {tool_call_id}")
        # Store the tool call for completion later
        self.current_tool_calls[tool_call_id] = {
            "tool_name": tool_name,
            "input": tool_input,
            "start_time": datetime.now(),
            "call_id": tool_call_id,
            "signature": tool_signature
        }
        
        logger.info(f"Captured tool start: {tool_name} with ID {tool_call_id}")
        return tool_call_id
    
    def capture_tool_end(self, tool_call_id: str, output: str, is_error: bool = False):
        """Capture tool completion for LLM context. 
        Important for cancelled chats since this removes and tracks running tool calls."""
        logger.debug(f"capture_tool_end called: tool_call_id={tool_call_id}, output_length={len(output)}")
        
        if tool_call_id not in self.current_tool_calls:
            logger.warning(f"Tool call {tool_call_id} not found in captured calls")
            return
            
        tool_info = self.current_tool_calls[tool_call_id]
        tool_name = tool_info["tool_name"]
        tool_input = tool_info["input"]
        run_id = tool_info.get("run_id")  # Get the run_id from the tool info
        
        logger.debug(f"Tool info: {tool_name}, input: {tool_input}, run_id: {run_id}")
        
        # Create the tool call message (for agent flow)
        tool_call_msg = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [{
                    "id": tool_call_id,
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                    },
                    "type": "function"
                }]
            },
            id=run_id
        )
        
        # Store original content for potential summarization
        original_content = output
        
        # Set threshold to 20k tokens (roughly equivalent to 80k characters)
        SUMMARIZATION_THRESHOLD_TOKENS = 10000
        
        # Store summarized content separately - DON'T create InternalToolMessage for agent flow
        content_tokens = count_tokens(original_content)
        if content_tokens > SUMMARIZATION_THRESHOLD_TOKENS:
            logger.info(f" Tool output length {content_tokens} tokens exceeds {SUMMARIZATION_THRESHOLD_TOKENS} token threshold, summarizing for LLM context")
            try:
                from ..llm import ModelConfig
                llm = LLMManager()
                summary = llm.summarize(original_content, model=ModelConfig.TOOL_OUTPUT_SUMMARIZATION_MODEL)
                summarized_content = summary + '\n\n[Summarized from longer output]'
                summary_tokens = count_tokens(summarized_content)
                logger.debug(f" SUMMARIZATION CREATED: {content_tokens} -> {summary_tokens} tokens")
                logger.debug(f" SUMMARY CONTENT: {summarized_content[:200]}...")
                
                self._store_summarized_result(
                    tool_call_id,
                    run_id,
                    {
                        'original_output': original_content,
                        'summarized_output': summarized_content,
                        'tool_name': tool_name,
                        'tool_input': tool_input,
                    },
                )
                logger.debug(f"Stored summarized content separately - will NOT go through agent flow")
                
            except Exception as e:
                logger.error(f"Failed to summarize tool output: {e}, using truncation instead")
                truncated_content = original_content[:5000] + '\n\n[Truncated due to summarization error]'
                self._store_summarized_result(
                    tool_call_id,
                    run_id,
                    {
                        'original_output': original_content,
                        'summarized_output': truncated_content,
                        'tool_name': tool_name,
                        'tool_input': tool_input,
                    },
                )
        
        # Always create regular ToolMessage with original content for agent flow
        # Use tool_call_id (call_xxx format) NOT run_id (run-xxx format)
        # This ensures proper matching in workflow._associate_tool_calls_with_output
        tool_result_msg = ToolMessage(
            content=original_content,
            tool_call_id=tool_call_id  # Must use tool_call_id for proper UI association
        )
        
        # DEBUG: Verify tool_call_id is correct immediately after creation
        logger.info(f"CREATED ToolMessage for {tool_name}: tool_call_id={tool_call_id}, actual_id={tool_result_msg.tool_call_id}")
        
        # LangGraph mutates tool_call_id during state updates for parallel tools
        # Store mapping from content hash to tool_call_id for restoration
        import hashlib
        content_hash = hashlib.md5(original_content.encode('utf-8')).hexdigest()
        self.content_to_tool_id[content_hash] = str(tool_call_id)
        logger.info(f"STORED content mapping: {content_hash[:16]}... → {tool_call_id}")
        
        # Set tool_call_id normally (will be corrupted by LangGraph, but we have content mapping)
        tool_result_msg.tool_call_id = str(tool_call_id)
        
        # Add both messages to collected messages for state (agent will see original content)
        self.collected_tool_messages.extend([tool_call_msg, tool_result_msg])
        logger.info(f"AFTER APPEND: ToolMessage has tool_call_id={tool_result_msg.tool_call_id}, content_hash={content_hash[:16]}...")
        logger.debug(f"Collected tool messages - total collected: {len(self.collected_tool_messages)}")
        
        # CRITICAL FIX: Mark as completed but DON'T remove yet
        # The with_completion_notification wrapper needs to access this entry for matching
        # The wrapper will mark it as 'completed' and then the cleanup happens there
        # This prevents the race condition where tool is removed before wrapper can process it
        logger.debug(f"Current tool calls: {self.current_tool_calls} and tool_call_id: {tool_call_id}")
        with self.lock: # Use lock for concurrent access
            if tool_call_id in self.current_tool_calls:
                tool_signature = self.current_tool_calls[tool_call_id]['signature']
                self.tool_execution_signatures.add(tool_signature)
                # Mark as completed but keep in current_tool_calls for wrapper matching
                self.current_tool_calls[tool_call_id]['completed'] = True
                self.current_tool_calls[tool_call_id]['completion_time'] = datetime.now()
                logger.debug(f"Marked tool call {tool_call_id} as completed (but kept in tracking for wrapper), remaining active: {len([k for k, v in self.current_tool_calls.items() if not v.get('completed')])}")
    
    def _cleanup_stale_completed_calls(self):
        """Remove completed tool calls that have been sitting in tracking for too long.
        This is a safety mechanism in case the wrapper didn't clean them up."""
        MAX_STALE_SECONDS = 60  # Clean up after 1 minute
        
        with self.lock:
            stale_ids = []
            now = datetime.now()
            
            for call_id, call_info in self.current_tool_calls.items():
                if call_info.get('completed'):
                    completion_time = call_info.get('completion_time')
                    if completion_time:
                        age_seconds = (now - completion_time).total_seconds()
                        if age_seconds > MAX_STALE_SECONDS:
                            stale_ids.append(call_id)
            
            for call_id in stale_ids:
                del self.current_tool_calls[call_id]
                logger.warning(f"Cleaned up stale completed tool call {call_id} (age > {MAX_STALE_SECONDS}s)")
            
            if stale_ids:
                logger.info(f"Cleaned up {len(stale_ids)} stale completed tool calls")

    def _store_summarized_result(self, tool_call_id: Optional[str], run_id: Optional[str], payload: Dict[str, Any]):
        """Store summarized tool output under both tool_call_id and run_id for compatibility."""
        if not hasattr(self, 'summarized_tool_results'):
            self.summarized_tool_results = {}

        if tool_call_id:
            self.summarized_tool_results[tool_call_id] = payload
        if run_id and run_id != tool_call_id:
            self.summarized_tool_results[run_id] = payload
    
    def get_content_to_tool_id_mapping(self):
        """Get the content hash to tool_call_id mapping for restoration."""
        return self.content_to_tool_id.copy()
    
    def get_collected_tool_messages(self):
        """Get all collected tool messages and clear the collection."""
        messages = self.collected_tool_messages.copy()
        self.collected_tool_messages.clear()
        
        # Clean up stale completed tool calls that weren't cleaned up by wrapper
        # This prevents memory leaks from orphaned entries
        self._cleanup_stale_completed_calls()
        
        logger.debug(f"get_collected_tool_messages called with {len(messages)} total messages")
        
        # DEBUG: Log all ToolMessage IDs and check additional_kwargs
        for i, msg in enumerate(messages):
            if isinstance(msg, ToolMessage):
                has_kwargs = hasattr(msg, 'additional_kwargs')
                backup_id = msg.additional_kwargs.get('_original_tool_call_id') if has_kwargs else None
                logger.info(f"GET_COLLECTED MESSAGE {i}: ToolMessage tool_call_id={msg.tool_call_id}, has_kwargs={has_kwargs}, backup={backup_id}")
        
        # Separate internal and external messages
        external_messages = []
        internal_messages = []
        
        for i, msg in enumerate(messages):
            is_internal = False
            if hasattr(msg, 'additional_kwargs') and msg.additional_kwargs:
                is_internal = msg.additional_kwargs.get('internal', False)
            
            msg_type = type(msg).__name__
            logger.debug(f"Message {i}: type={msg_type}, has_additional_kwargs={hasattr(msg, 'additional_kwargs')}, internal={is_internal}")
            
            if is_internal:
                internal_messages.append(msg)
                logger.debug(f"INTERNAL MESSAGE DETECTED: {msg_type} - FILTERING OUT FROM WEBSOCKET")
                logger.debug(f"Internal message content preview: {msg.content[:100]}...")
            else:
                external_messages.append(msg)
                logger.debug(f"External message: {msg_type} - will go to WebSocket")
                
        # DON'T call LLMContextManager.save_context_history() - this might be causing the leak
        # Instead, just keep internal messages for LLM context in memory
        if internal_messages:
            logger.info(f"CRITICAL: {len(internal_messages)} internal messages detected - SHOULD NOT reach frontend")
            # Store them in a class attribute for later use by LLM context
            if not hasattr(self, 'internal_messages_for_llm'):
                self.internal_messages_for_llm = []
            self.internal_messages_for_llm.extend(internal_messages)
        
        # CANCELLATION FIX: Handle tools that were interrupted mid-execution
        # These tools are still in current_tool_calls but have no ToolMessage
        # Create "cancelled" ToolMessages for them so UI doesn't show "running" forever
        with self.lock:
            # Extract tool_call_ids from existing ToolMessages
            existing_tool_call_ids = set()
            for msg in external_messages:
                if isinstance(msg, ToolMessage) and hasattr(msg, 'tool_call_id'):
                    existing_tool_call_ids.add(msg.tool_call_id)
            
            # Find interrupted tools (in current_tool_calls but no ToolMessage)
            interrupted_tools = []
            for call_id, tool_info in list(self.current_tool_calls.items()):
                if call_id not in existing_tool_call_ids:
                    # This tool was interrupted - has no ToolMessage
                    interrupted_tools.append((call_id, tool_info))
                    logger.warning(f"INTERRUPTED TOOL DETECTED: {call_id} ({tool_info.get('tool_name')}) - creating cancellation message")
            
            # Create cancellation messages for interrupted tools
            for call_id, tool_info in interrupted_tools:
                tool_name = tool_info.get('tool_name', 'unknown')
                tool_input = tool_info.get('input', {})
                run_id = tool_info.get('run_id')
                
                # Create AIMessage with tool call (parent message)
                tool_call_msg = AIMessage(
                    content="",
                    additional_kwargs={
                        "tool_calls": [{
                            "id": call_id,
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                            },
                            "type": "function"
                        }]
                    },
                    id=run_id
                )
                
                # Create ToolMessage with cancellation status
                cancellation_result = {
                    "success": False,
                    "cancelled": True,
                    "message": "Tool execution was cancelled before completion",
                    "tool_name": tool_name,
                    "input": tool_input
                }
                
                tool_result_msg = ToolMessage(
                    content=json.dumps(cancellation_result),
                    tool_call_id=call_id
                )
                
                # Add both messages to external_messages
                external_messages.extend([tool_call_msg, tool_result_msg])
                logger.info(f"Created cancellation ToolMessage for interrupted tool {call_id} ({tool_name})")
                
                # Remove from current_tool_calls
                del self.current_tool_calls[call_id]
        
        # Return only external messages that can be safely added to state and sent via WebSocket
        logger.info(f"FINAL RESULT: Returning {len(external_messages)} external messages (filtered out {len(internal_messages)} internal, added {len(interrupted_tools)} cancellation messages)")
        logger.info(f"External messages types: {[type(msg).__name__ for msg in external_messages]}")
        
        return external_messages
    
    def capture_agent_response(self, response_text: str):
        """Capture the final agent response after tool executions."""
        # REMOVED: This method was causing duplicate saves
        # The workflow's final save operation already includes all messages including agent responses
        # Keeping this would load existing context, add the response, and save again - causing duplication
        logger.info(f"Skipping duplicate agent response capture - handled by workflow final save")
        pass

from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import StateGraph
from langgraph.graph import START, END
from chat.backend.agent.utils.safe_memory_saver import SafeMemorySaver
from langchain_core.runnables.config import RunnableConfig
from langchain_core.messages import AIMessageChunk, AIMessage
from chat.backend.agent.agent import Agent
from chat.backend.agent.utils.state import State
import logging
import json
import asyncio
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _extract_text_from_content(content: Any, include_thinking: bool = False) -> str:
    """
    Extract text content from message content, handling Gemini thinking model responses.
    
    Gemini thinking models return content as a list of blocks with types:
    - {"type": "thinking", "thinking": "..."} - reasoning/thinking blocks (extracted)
    - {"type": "text", "text": "..."} - actual text response (extracted)
    
    For RCA background chats, thinking blocks contain the investigation progress,
    so we extract them as part of the thought stream.
    
    Args:
        content: Message content (can be string, list, or other types)
        
    Returns:
        Extracted text as string
    """
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type", "")
                
                # Extract text from both thinking and text blocks
                # thinking blocks: use 'thinking' key
                # text blocks: use 'text' key
                if part_type in ("thinking", "reasoning") and include_thinking:
                    thinking_text = part.get("thinking", "")
                    if thinking_text:
                        text_parts.append(str(thinking_text))
                elif part_type == "text" or not part_type:
                    # Regular text content
                    text = part.get("text", "")
                    if text:
                        text_parts.append(str(text))
            elif isinstance(part, str):
                text_parts.append(part)
        return "".join(text_parts)
    return str(content)


class Workflow:
    def __init__(self, agent: Agent, session_id: str):
        self.agent = agent
        self.memory = SafeMemorySaver()
        self.config = RunnableConfig({"configurable": {"thread_id": session_id}})
        self.app = self.get_compiled_workflow()
        self._last_state = None
        self._ui_state = None  # Store UI state to save with messages

    async def _wait_for_ongoing_tool_calls(self):
        """Waits for any tool calls that are currently in progress to complete."""
        tool_capture = getattr(self.agent, 'tool_capture_instance', None)
        if not tool_capture:
            logger.info("No tool capture instance found, cannot wait for tool calls.")
            return

        # Initial check for ongoing calls
        with tool_capture.lock:
            ongoing_calls = list(tool_capture.current_tool_calls.keys())

        if not ongoing_calls:
            logger.info("No ongoing tool calls detected at the start of wait.")
            return

        logger.info(f"Waiting for {len(ongoing_calls)} tool call(s) to complete: {ongoing_calls}")
        
        POLL_INTERVAL = 0.5  # seconds
        MAX_WAIT_TIME = 30   # seconds
        time_waited = 0

        while time_waited < MAX_WAIT_TIME:
            await asyncio.sleep(POLL_INTERVAL)
            time_waited += POLL_INTERVAL

            with tool_capture.lock:
                # Check if the initial ongoing calls are still present
                still_ongoing = [call_id for call_id in ongoing_calls if call_id in tool_capture.current_tool_calls]
            
            if not still_ongoing:
                logger.info(f"All tracked tool calls have completed after {time_waited:.1f}s.")
                return
            
            logger.debug(f"Still waiting for {len(still_ongoing)} tool call(s): {still_ongoing}")

    def _create_workflow(self) -> StateGraph:
        """Create and configure the workflow graph."""
        workflow = StateGraph(State)
        workflow.add_node("agentic_tool_flow", self.agent.agentic_tool_flow)
        workflow.add_edge(START, "agentic_tool_flow") 
        workflow.add_edge("agentic_tool_flow", END)
        return workflow

    def get_compiled_workflow(self) -> CompiledStateGraph:
        """Compile the workflow graph"""
        return self._create_workflow().compile(checkpointer=self.memory)

    def _get_state_attr(self, state, attr_name, default=None):
        """Helper method to access state attributes consistently."""
        if hasattr(state, 'get'):
            return state.get(attr_name, default)
        return getattr(state, attr_name, default)

    def _set_state_attr(self, state, attr_name, value):
        """Helper method to set state attributes consistently."""
        if hasattr(state, 'get'):
            state[attr_name] = value
        else:
            setattr(state, attr_name, value)

    def _scan_for_placeholders(self, messages) -> bool:
        """Detect placeholder tokens in AI messages to trigger follow-up guidance."""
        if not messages:
            return False

        placeholder_tokens = [
            "<project", "project-id", "your-project", "replace", "todo", "subscription id", "subscription-id", "account id",
        ]

        def _content_to_text(content) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                collected = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        collected.append(str(block.get('text', '')))
                    else:
                        collected.append(str(block))
                return " ".join(collected)
            return str(content or "")

        for msg in messages:
            if isinstance(msg, AIMessage):
                text = _content_to_text(getattr(msg, 'content', ''))
                lowered = text.lower()
                if any(token in lowered for token in placeholder_tokens):
                    return True
        return False

    def _extract_last_tool_failure(self, messages) -> Optional[dict]:
        """Find the most recent tool call that failed and return summary metadata."""
        if not messages:
            return None

        last_failure = None

        for msg in messages:
            from langchain_core.messages import ToolMessage

            if isinstance(msg, ToolMessage):
                try:
                    import json as _json
                    payload = _json.loads(getattr(msg, 'content', '{}'))
                except Exception:
                    payload = {}

                # Handle case where payload is a list (e.g., MCP tool responses)
                if isinstance(payload, list):
                    continue  # Lists don't contain failure status metadata
                
                status = payload.get('status') or payload.get('success')
                if status in (False, 'failed', 'error'):
                    last_failure = {
                        'tool_name': payload.get('tool_name') or getattr(msg, 'name', None),
                        'message': payload.get('message') or payload.get('error') or str(payload),
                        'command': payload.get('final_command') or payload.get('command') or '',
                    }

        return last_failure

    def _handle_legacy_session_migration(self, input_state, LLMContextManager):
        """Handle migration of legacy sessions."""
        try:
            from utils.db.connection_pool import db_pool
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SET myapp.current_user_id = %s;", (input_state.user_id,))
                cursor.execute("""
                    SELECT messages FROM chat_sessions 
                    WHERE id = %s AND user_id = %s AND is_active = true
                """, (input_state.session_id, input_state.user_id))
                
                result = cursor.fetchone()
                if result and result[0]:
                    ui_messages = result[0] if isinstance(result[0], list) else json.loads(result[0])
                    if ui_messages:
                        logger.debug(f"[WORKFLOW FINAL] Migrating legacy session")
                        LLMContextManager.migrate_legacy_session(
                            input_state.session_id, 
                            input_state.user_id, 
                            ui_messages
                        )
                        return True
        except Exception as e:
            logger.warning(f"Failed to migrate legacy session {input_state.session_id}: {e}")
        return False

    def _process_tool_calls_from_chunk(self, msg_chunk, tool_capture):
        """Process tool calls from a message chunk and capture them."""
        if not tool_capture or not hasattr(msg_chunk, 'additional_kwargs'):
            return
            
        tool_calls = msg_chunk.additional_kwargs.get('tool_calls', [])
        if not tool_calls:
            return
            
        run_id = getattr(msg_chunk, 'id', None)
        if not run_id:
            logger.warning(f"WORKFLOW: AIMessage chunk with tool calls has no ID, cannot track properly")
            return
            
        logger.debug(f" WORKFLOW: Capturing {len(tool_calls)} tool calls for run ID: {run_id}")
        for i, tool_call in enumerate(tool_calls):
            tool_name = tool_call.get('function', {}).get('name', 'unknown')
            tool_args = tool_call.get('function', {}).get('arguments', '{}')
            tool_call_id = tool_call.get('id', f"{run_id}_{i}")
            tool_call.setdefault('timestamp', datetime.now().isoformat())

            if tool_call_id is None:
                logger.debug("WORKFLOW: Skipping tool call without id (run_id=%s, index=%s)", run_id, i)
                continue
            
            try:
                tool_input = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
            except:
                tool_input = tool_args
            
            logger.debug(f"WORKFLOW: Capturing tool call: '{tool_name}' with tool_call_id={tool_call_id}, run_id={run_id}")

            existing_call = tool_capture.current_tool_calls.get(tool_call_id)

            def _is_meaningful(value):
                if isinstance(value, dict):
                    return bool(value)
                if isinstance(value, str):
                    return bool(value.strip())
                return value is not None

            meaningful_input = _is_meaningful(tool_input)

            if existing_call:
                # Preserve earlier metadata but refresh with richer payloads when available
                existing_call.setdefault("tool_name", tool_name)
                existing_call.setdefault("call_id", tool_call_id)
                existing_call.setdefault("run_id", run_id)
                existing_call.setdefault("start_time", datetime.now())

                if meaningful_input:
                    existing_call["input"] = tool_input
                    existing_call["signature"] = self._build_tool_signature(tool_name, tool_input)
            else:
                signature = self._build_tool_signature(tool_name, tool_input) if meaningful_input else self._build_tool_signature(tool_name, "__placeholder__")
                tool_capture.current_tool_calls[tool_call_id] = {
                    "tool_name": tool_name,
                    "input": tool_input if meaningful_input else tool_input,
                    "start_time": datetime.now(),
                    "call_id": tool_call_id,
                    "run_id": run_id,
                    "signature": signature
                }

    def _merge_tool_call_args(self, existing_args, new_args):
        """Merge tool call arguments intelligently."""
        if isinstance(existing_args, dict) and isinstance(new_args, dict):
            return {**existing_args, **new_args}
        elif isinstance(existing_args, str) and isinstance(new_args, str):
            return existing_args + new_args
        elif isinstance(existing_args, str) and isinstance(new_args, dict):
            return new_args  # Dict is more complete
        elif isinstance(existing_args, dict) and isinstance(new_args, str):
            if not existing_args:  # Empty dict
                return new_args
            return existing_args  # Keep existing dict
        else:
            return str(existing_args) + str(new_args)

    def _build_tool_signature(self, tool_name: str, tool_input: Any) -> str:
        """Create a stable signature for matching tool executions."""
        try:
            if isinstance(tool_input, dict):
                # Remove None values to avoid signature churn and ensure deterministic ordering
                filtered = {k: v for k, v in tool_input.items() if v is not None}
                serialized = json.dumps(filtered, sort_keys=True)
            else:
                serialized = str(tool_input)
        except (TypeError, ValueError):
            serialized = str(tool_input)
        return f"{tool_name}_{serialized}"

    def _process_tool_calls_for_chunk(self, msg, builder):
        """Process tool calls from a message chunk and update the builder."""
        # Handle tool_calls from the primary attribute
        if getattr(msg, 'tool_calls', None):
            self._process_tool_calls_source(msg.tool_calls, builder, "primary")
        
        # Handle tool_calls from additional_kwargs for backward compatibility
        if hasattr(msg, 'additional_kwargs') and msg.additional_kwargs.get('tool_calls'):
            self._process_tool_calls_source(msg.additional_kwargs['tool_calls'], builder, "additional_kwargs")

    def _process_tool_calls_source(self, tool_calls, builder, source_name):
        """Process tool calls from a specific source (primary or additional_kwargs)."""
        for incoming_tc in tool_calls:
            tc_index = incoming_tc.get('index', 0)
            tc_id = incoming_tc.get('id')
            if not incoming_tc.get('timestamp'):
                fallback_ts = None
                if isinstance(builder.get("additional_kwargs"), dict):
                    fallback_ts = builder["additional_kwargs"].get("timestamp")
                incoming_tc["timestamp"] = fallback_ts or datetime.now().isoformat()
            
            # Extract function info (handle both direct and function wrapper formats)
            if 'function' in incoming_tc:
                function_info = incoming_tc.get('function', {})
                tc_name = function_info.get('name')
                tc_args = function_info.get('arguments', '')
            else:
                tc_name = incoming_tc.get('name')
                tc_args = incoming_tc.get('args', '')
            
            # Find existing tool call with same index or create new one
            existing_tc = None
            for existing in builder["tool_calls"]:
                if existing.get('index') == tc_index:
                    existing_tc = existing
                    break
            
            if existing_tc:
                # Accumulate arguments for streaming tool calls
                existing_args = existing_tc.get('args', '')
                existing_tc['args'] = self._merge_tool_call_args(existing_args, tc_args)
                
                # Update other fields if they become available
                if tc_id and not existing_tc.get('id'):
                    existing_tc['id'] = tc_id
                if tc_name and not existing_tc.get('name'):
                    existing_tc['name'] = tc_name
                elif tc_name and existing_tc.get('name'):
                    # Handle case where tool name might also be streamed
                    existing_name = existing_tc.get('name', '')
                    if not existing_name.endswith(tc_name):
                        existing_tc['name'] = existing_name + tc_name
                if incoming_tc.get("timestamp") and not existing_tc.get("timestamp"):
                    existing_tc["timestamp"] = incoming_tc.get("timestamp")
            else:
                # Create new tool call entry
                # Use the tool call's own ID (tc_id), NOT the message builder's ID
                # Using builder["id"] creates duplicates with different IDs for the same tool call
                builder["tool_calls"].append({
                    'index': tc_index,
                    'id': tc_id,  # Use tool call's ID, not message ID
                    'name': tc_name,
                    'args': tc_args,
                    'type': incoming_tc.get('type', 'function'),
                    'timestamp': incoming_tc.get('timestamp'),
                })

    def _clean_tool_calls(self, tool_calls):
        """Clean and deduplicate tool calls."""
        # Step 1: de-duplicate tool calls by ID
        deduped_by_id = {}
        # helper to test empty args
        def _is_empty(v):
            return v in ("{}", "", None) or (isinstance(v, dict) and not v)

        for tc in tool_calls:
            tc_id_key = tc.get("id") or f"idx_{tc.get('index', 0)}"
            if tc_id_key in deduped_by_id:
                prev = deduped_by_id[tc_id_key]
                prev_args = prev.get("args")
                new_args = tc.get("args")
                # If both have a command and they differ, keep the original (prev) intact
                try:
                    if (
                        isinstance(prev_args, dict)
                        and isinstance(new_args, dict)
                        and "command" in prev_args
                        and "command" in new_args
                        and prev_args["command"] != new_args["command"]
                    ):
                        # Do not overwrite; keep prev_args
                        pass
                    else:
                        prev["args"] = self._merge_tool_call_args(prev_args, new_args)
                except Exception:
                    # Fallback to safe merge if any unexpected structure
                    prev["args"] = self._merge_tool_call_args(prev_args, new_args)
                
                # Merge other simple fields
                for key in ["confirmation_id", "status", "name", "type"]:
                    if not prev.get(key) and tc.get(key):
                        prev[key] = tc[key]
                if tc.get("timestamp"):
                    if not prev.get("timestamp"):
                        prev["timestamp"] = tc["timestamp"]
                    else:
                        try:
                            prev_ts = datetime.fromisoformat(prev["timestamp"])
                            tc_ts = datetime.fromisoformat(tc["timestamp"])
                            if tc_ts < prev_ts:
                                prev["timestamp"] = tc["timestamp"]
                        except Exception:
                            pass
                
                deduped_by_id[tc_id_key] = prev
            else:
                deduped_by_id[tc_id_key] = tc
        
        # Step 2: merge records that share the same index
        index_map = {}
        for key, tc in list(deduped_by_id.items()):
            idx = tc.get('index', 0)
            if idx in index_map:
                primary = index_map[idx]
                
                # If both records have IDs and they differ, treat them as separate parallel calls
                primary_id = primary.get('id')
                current_id = tc.get('id')

                # Heuristic: If we have two IDs but one is a placeholder like "tool_0_*" and
                # the other is the real run_id (starts with "run-"), treat them as the SAME
                # tool call and use the run_id as the canonical ID. This avoids keeping both
                # entries and later losing the more useful run_id.
                if primary_id and current_id and primary_id != current_id:
                    placeholder_ids = [pid for pid in (primary_id, current_id) if pid.startswith('tool_')]
                    run_ids = [rid for rid in (primary_id, current_id) if rid.startswith('run-')]

                    if placeholder_ids and run_ids:
                        # Prefer the record that has the run_id as the canonical entry
                        preferred_id = run_ids[0]
                        if primary_id.startswith('tool_'):
                            # Swap so that primary becomes the run_id record
                            primary, tc = tc, primary
                            index_map[idx] = primary
                            deduped_by_id[preferred_id] = primary
                            # Remove the placeholder key if it exists in deduped_by_id
                            if primary_id in deduped_by_id:
                                deduped_by_id.pop(primary_id, None)
                        # After ensuring primary holds the run_id record, merge as usual
                    else:
                        # Different real IDs -> treat as parallel calls
                        continue
                
                # Prefer record that has a real id when one is missing
                if primary_id is None and current_id:
                    primary, tc = tc, primary
                    index_map[idx] = primary
                    deduped_by_id[key] = primary
                
                # Merge args
                p_args = primary.get('args')
                t_args = tc.get('args')
                if _is_empty(p_args) and not _is_empty(t_args):
                    primary['args'] = t_args
                else:
                    primary['args'] = self._merge_tool_calls_args(p_args, t_args)
                
                # Merge other meta fields
                for meta_key in ['confirmation_id', 'status', 'timestamp', 'name', 'type']:
                    if not primary.get(meta_key) and tc.get(meta_key):
                        primary[meta_key] = tc[meta_key]
                
                # Drop duplicate entry if it's idx_x
                if key.startswith('idx_'):
                    deduped_by_id.pop(key, None)
            else:
                index_map[idx] = tc
        
        # Clean and format final tool calls
        cleaned_tool_calls = []
        for tc in deduped_by_id.values():
            args = tc.get('args')
            
            # AIMessage requires args to be a dict, not a string
            # Parse args if it's a string that looks like JSON
            if isinstance(args, str):
                args_stripped = args.strip()
                
                # VALIDATION: Reject malformed args that contain tool output or internal data
                # These are signs of corrupted tool call data and should not be wrapped
                suspicious_patterns = ['"user_id":', '"session_id":', '"resource_id":', '"auth_method":']
                is_suspicious = any(pattern in args_stripped for pattern in suspicious_patterns)
                
                if is_suspicious:
                    # This looks like tool OUTPUT or internal metadata, not valid tool call args
                    # Extract just the JSON object if it exists, otherwise use empty dict
                    logger.warning(f"Detected malformed tool_call args with internal data, attempting to clean: {args_stripped[:150]}...")
                    
                    # Try to extract the first valid JSON object before the suspicious data
                    try:
                        # Find the first complete JSON object
                        brace_count = 0
                        json_end = -1
                        for i, char in enumerate(args_stripped):
                            if char == '{':
                                brace_count += 1
                            elif char == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    json_end = i + 1
                                    break
                        
                        if json_end > 0:
                            clean_json = args_stripped[:json_end]
                            args = json.loads(clean_json)
                            logger.info(f"Successfully extracted clean JSON from malformed args")
                        else:
                            # Can't extract valid JSON, use empty dict
                            logger.error(f"Cannot extract valid JSON from malformed args, using empty dict")
                            args = {}
                    except (json.JSONDecodeError, Exception) as e:
                        logger.error(f"Failed to clean malformed args: {e}, using empty dict")
                        args = {}
                
                elif args_stripped.startswith('{') and args_stripped.endswith('}'):
                    try:
                        # Try to parse as JSON (strip whitespace first)
                        args = json.loads(args_stripped)
                    except json.JSONDecodeError:
                        # If JSON parsing fails and it's not suspicious, treat as raw content
                        # This handles cases where args contains non-JSON content (e.g., terraform HCL for iac_tool write actions)
                        logger.warning(f"Failed to parse tool_call args as JSON for non-suspicious content: {args_stripped[:100]}...")
                        args = {"content": args_stripped}
                else:
                    # Non-JSON string, wrap it appropriately based on context
                    if len(args_stripped) > 0:
                        args = {"value": args_stripped}
                    else:
                        args = {}
            elif args is None or args == "":
                # Empty args should be an empty dict
                args = {}
            
            # Heuristic: if provider is gcp and command lacks 'gcloud', prefix it
            if isinstance(args, dict) and args.get('provider') == 'gcp':
                cmd = args.get('command')
                if isinstance(cmd, str) and not cmd.strip().startswith('gcloud'):
                    args['command'] = f"gcloud {cmd.strip()}"
            
            # Final validation: Ensure args is always a dict for AIMessage compatibility
            if not isinstance(args, dict):
                logger.error(f"Tool call args is not a dict after cleanup: {type(args)}, forcing empty dict")
                args = {}
            
            cleaned_tc = {
                'id': tc.get('id'),
                'name': tc.get('name'),
                'args': args,
                'type': tc.get('type', 'function')
            }
            # Remove None values
            cleaned_tc = {k: v for k, v in cleaned_tc.items() if v is not None}
            cleaned_tool_calls.append(cleaned_tc)
        
        return cleaned_tool_calls

    def _merge_tool_calls_args(self, prev_args, new_args):
        """Merge tool call arguments with type-aware logic."""
        def _is_empty(v):
            return v in ("{}", "", None) or (isinstance(v, dict) and not v)
        
        if _is_empty(prev_args) and not _is_empty(new_args):
            return new_args
        elif not _is_empty(prev_args) and _is_empty(new_args):
            return prev_args
        elif isinstance(prev_args, dict) and isinstance(new_args, dict):
            return {**prev_args, **new_args}
        elif isinstance(prev_args, str) and isinstance(new_args, str):
            return new_args if len(new_args) > len(prev_args) else prev_args
        else:
            return new_args if isinstance(new_args, dict) else prev_args

    def _create_ai_message_from_builder(self, builder, cleaned_tool_calls):
        """Create an AIMessage from a chunk builder."""
        return AIMessage(
            content=builder["content"],
            additional_kwargs=builder["additional_kwargs"],
            response_metadata=builder["response_metadata"],
            id=builder["id"],
            tool_calls=cleaned_tool_calls,
        )

    def _deduplicate_messages(self, consolidated_messages):
        """Remove duplicate messages while properly handling parallel tool calls."""
        final_messages = []
        seen_ai_messages = {}
        seen_ai_content = set()
        seen_tool_messages = {}
        seen_other_messages = set()
        
        for msg in consolidated_messages:
            msg_type = type(msg).__name__
            msg_id = getattr(msg, 'id', None)
            
            if 'AI' in msg_type:
                has_content = getattr(msg, 'content', None)
                has_tool_calls = getattr(msg, 'tool_calls', None)
                
                # Skip if we've seen this exact ID before
                if msg_id and msg_id in seen_ai_messages:
                    logger.debug(f"CONSOLIDATION: Skipping duplicate AI message with ID: {msg_id}")
                    continue
                
                # Handle AI messages with tool calls
                if has_tool_calls:
                    current_ids = {tc.get('id', '') for tc in has_tool_calls}
                    
                    # Check for overlapping tool call IDs and replace older messages
                    for seen_msg in seen_ai_messages.values():
                        seen_tool_calls = getattr(seen_msg, 'tool_calls', None)
                        if seen_tool_calls:
                            seen_ids = {tc.get('id', '') for tc in seen_tool_calls}
                            if seen_ids & current_ids:
                                seen_msg_id = getattr(seen_msg, 'id', None)
                                if seen_msg_id in seen_ai_messages:
                                    final_messages[:] = [m for m in final_messages if getattr(m, 'id', None) != seen_msg_id]
                                    del seen_ai_messages[seen_msg_id]
                                break
                
                # Handle AI messages with content but no tool calls
                elif has_content and not has_tool_calls:
                    content_signature = str(has_content).strip()
                    if content_signature in seen_ai_content:
                        logger.debug(f"CONSOLIDATION: Skipping duplicate AI response content: '{content_signature[:50]}...'")
                        continue
                    seen_ai_content.add(content_signature)
                
                # Keep AI messages that have content OR tool calls
                if has_content or has_tool_calls:
                    final_messages.append(msg)
                    if msg_id:
                        seen_ai_messages[msg_id] = msg
                    logger.debug(f"CONSOLIDATION: Kept AI message (ID: {msg_id}) with content={bool(has_content)}, tool_calls={bool(has_tool_calls)}")
                    
            elif 'Tool' in msg_type:
                # Handle tool messages
                tool_call_id = getattr(msg, 'tool_call_id', None)
                command = None
                try:
                    content = json.loads(getattr(msg, 'content', '{}'))
                    command = content.get('final_command')
                except (json.JSONDecodeError, AttributeError):
                    pass

                unique_key = command if command else getattr(msg, 'id', None)
                tool_signature = (tool_call_id, unique_key)
                
                # DEBUG: Log message details during dedup
                logger.info(f"DEDUP PROCESSING: ToolMessage with tool_call_id={tool_call_id}, command={command[:50] if command else 'None'}")

                if tool_call_id and unique_key and tool_signature in seen_tool_messages:
                    logger.info(f"DEDUP SKIP: Duplicate found for {tool_call_id}")
                    continue
                
                final_messages.append(msg)
                if tool_call_id and unique_key:
                    seen_tool_messages[tool_signature] = msg
                    logger.info(f"DEDUP ADDED: Stored {tool_call_id} in seen_tool_messages")
                    
            else:
                # Handle other message types
                msg_content = str(getattr(msg, 'content', ''))
                msg_signature = f"{msg_type}:{msg_content}"
                if hasattr(msg, 'additional_kwargs') and isinstance(msg.additional_kwargs, dict):
                    timestamp = msg.additional_kwargs.get('timestamp', '')
                    if timestamp:
                        msg_signature = f"{msg_type}:{msg_content}:{timestamp}"
                
                if msg_signature in seen_other_messages:
                    logger.debug(f"CONSOLIDATION: Skipping duplicate {msg_type} message")
                    continue
                
                final_messages.append(msg)
                seen_other_messages.add(msg_signature)
        
        return final_messages

    async def stream(self, input_state: State):
        """Stream the workflow with enhanced tool interaction capture"""
        # Import here to avoid circular dependency
        from chat.backend.agent.utils.llm_context_manager import LLMContextManager
        from chat.backend.agent.utils.chat_context_manager import ChatContextManager
        from chat.backend.agent.utils.tool_context_capture import ToolContextCapture
        
        # Initialize tool context capture for this session
        tool_capture = None
        if input_state.session_id and input_state.user_id:
            tool_capture = ToolContextCapture(input_state.session_id, input_state.user_id)
            self.agent.set_tool_capture(tool_capture)
            
            # Set workflow context for tools to access during confirmation
            from chat.backend.agent.tools.cloud_tools import _set_ctx
            _set_ctx("workflow", self)
        
        # Load existing context if session_id is provided
        if input_state.session_id and input_state.user_id:
            import time as _time
            _ctx_start = _time.perf_counter()
            existing_context = LLMContextManager.load_context_history(
                input_state.session_id, 
                input_state.user_id
            )
            _ctx_ms = (_time.perf_counter() - _ctx_start) * 1000.0
            logger.info(f"Context load took {_ctx_ms:.1f} ms for session {input_state.session_id}")
            
            if existing_context:
                # Combine existing context with new messages
                combined_messages = []
                combined_messages.extend(existing_context)
                combined_messages.extend(input_state.messages)
                input_state.messages = combined_messages
                logger.info(f"Loaded {len(existing_context)} existing messages for session {input_state.session_id}, total messages: {len(input_state.messages)}")
                
                # Handle attachments
                original_attachments = getattr(input_state, 'attachments', None)
                previous_attachments = None
                for msg in existing_context:
                    if hasattr(msg, 'attachments') and msg.attachments:
                        previous_attachments = msg.attachments
                        break
                
                if not original_attachments and previous_attachments:
                    input_state.attachments = previous_attachments
                    logger.info(f"Carried forward {len(previous_attachments)} attachments from previous context")
            else:
                # Check for legacy session migration
                if self._handle_legacy_session_migration(input_state, LLMContextManager):
                    # Try loading context again after migration
                    existing_context = LLMContextManager.load_context_history(
                        input_state.session_id, 
                        input_state.user_id
                    )
                    if existing_context:
                        combined_messages = []
                        combined_messages.extend(existing_context)
                        combined_messages.extend(input_state.messages)
                        input_state.messages = combined_messages
                        logger.info(f"Migrated and loaded {len(existing_context)} messages for legacy session {input_state.session_id}")
        
        # Log initial state
        logger.info(f"Starting workflow with session_id={input_state.session_id}, user_id={input_state.user_id}")
        
        # Stream the workflow using astream_events for token-level streaming
        import time as _time
        _t0_first = _time.perf_counter()
        _first_event = True
        _token_count = 0
        _event_count = 0
        
        try:
            async for event in self.app.astream_events(input_state, self.config, version="v2"):
                _event_count += 1
                event_type = event.get("event")
                event_name = event.get("name", "")
                
                if _first_event:
                    _first_event = False
                    _t_first_ms = (_time.perf_counter() - _t0_first) * 1000.0
                    logger.info(f"Time to first stream event: {_t_first_ms:.1f} ms")
                
                # Log first few events for debugging
                if _event_count <= 5:
                    logger.info(f"[WORKFLOW STREAM] Event #{_event_count}: type={event_type}, name={event_name}")
                
                # Handle token streaming from LLM
                if event_type == "on_chat_model_stream":
                    _token_count += 1
                    chunk_data = event.get("data", {})
                    chunk_obj = chunk_data.get("chunk")
                    if chunk_obj and hasattr(chunk_obj, 'content') and chunk_obj.content:
                        # Extract text content (handles Gemini thinking model list format)
                        content = _extract_text_from_content(chunk_obj.content)
                        
                        # Only yield if we have actual text content
                        if content:
                            yield ("token", content)
                            if _token_count <= 5:
                                logger.debug(f"[WORKFLOW STREAM] Token #{_token_count}: '{content[:30]}'")
                
                # Capture state from chain end events
                elif event_type == "on_chain_end" and event_name == "LangGraph":
                    output_data = event.get("data", {}).get("output")
                    if output_data:
                        self._last_state = output_data
                        logger.debug(f"[WORKFLOW STREAM] Captured final state from chain end")
                        # Yield values event for compatibility
                        yield ("values", output_data)
                
                # Handle tool calls
                elif event_type == "on_chat_model_end":
                    chunk_data = event.get("data", {})
                    output = chunk_data.get("output")
                    if output and hasattr(output, 'tool_calls') and output.tool_calls:
                        # Process tool calls
                        self._process_tool_calls_from_chunk(output, tool_capture)
                        logger.debug(f"[WORKFLOW STREAM] Detected {len(output.tool_calls)} tool calls")
                
                # Periodic state snapshots (for incremental saves)
                elif event_type == "on_chain_stream":
                    stream_data = event.get("data", {}).get("chunk")
                    if stream_data and isinstance(stream_data, dict):
                        # Update state with intermediate results
                        if self._last_state is None:
                            self._last_state = stream_data
                        elif hasattr(self._last_state, 'get'):
                            self._last_state.update(stream_data)
                
            logger.info(f"[WORKFLOW STREAM] Completed: {_event_count} events, {_token_count} tokens streamed")
        except Exception as stream_exception:
            logger.error(f"[WORKFLOW STREAM ERROR] Exception in workflow stream for session {input_state.session_id}: {stream_exception}", exc_info=True)
            raise
        
        # Consolidate message chunks and save final state
        self._consolidate_message_chunks()
        
        # Save final state (only if we have more than just the initial user message)
        if self._last_state and input_state.session_id and input_state.user_id:
            session_id = self._get_state_attr(self._last_state, 'session_id')
            user_id = self._get_state_attr(self._last_state, 'user_id')
            messages = self._get_state_attr(self._last_state, 'messages', [])
            
            # Only save if we have more than just the initial user message (which was saved immediately)
            if session_id and user_id and messages and len(messages) > 1:
                messages_full = messages
                messages_for_context = messages_full
                # Check if context compression is needed
                model = self._get_state_attr(self._last_state, 'model')
                if model:
                    compressed_messages, was_compressed = ChatContextManager.compress_context_if_needed(
                        session_id, user_id, messages_full, model
                    )
                    if was_compressed:
                        messages_for_context = compressed_messages
                        logger.info(f"Context compression: {len(compressed_messages)} messages preserved for session {session_id}")
                
                # Save context history (complete conversation including AI responses and tool calls)
                success = LLMContextManager.save_context_history(session_id, user_id, messages_for_context, tool_capture)
                if success:
                    logger.debug(f"[WORKFLOW FINAL] Successfully saved final context for session {session_id} ({len(messages_for_context)} messages)")
                else:
                    logger.warning(f"[WORKFLOW FINAL] Failed to save final context for session {session_id}")
                
                # Save UI-formatted messages (complete conversation for UI display)
                tool_capture = getattr(self.agent, 'tool_capture_instance', None)
                ui_messages = self._convert_to_ui_messages(messages_full, tool_capture)
                ui_success = self._save_ui_messages(session_id, user_id, ui_messages, self._ui_state)
                logger.debug(f"[WORKFLOW FINAL] UI messages save success: {ui_success}")
            elif session_id and user_id and messages and len(messages) == 1:
                logger.debug(f"[WORKFLOW FINAL] Skipping save - only user message present (already saved immediately)")
            else:
                logger.debug(f"[WORKFLOW FINAL] Skipping save - no messages to save")

    def _consolidate_message_chunks(self):
        """Consolidate AIMessageChunk objects into proper AIMessage objects."""
        if self._last_state is None:
            return
            
        messages = self._get_state_attr(self._last_state, 'messages', [])
        if not messages:
            return
        
        # Restore original tool_call_ids using ORDER-AWARE matching
        # LangGraph mutates tool_call_id during state updates for parallel/repeated tools
        # Match tool_calls from AIMessages to ToolMessages in sequential order
        
        import hashlib
        import json as json_lib
        
        # Build ordered list of tool_call_ids from AIMessages (execution order)
        # Use dict to preserve order while deduplicating
        ordered_tool_ids_dict = {}
        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if isinstance(tool_call, dict):
                        tool_id = tool_call.get('id')
                        if tool_id and tool_id not in ordered_tool_ids_dict:
                            ordered_tool_ids_dict[tool_id] = True
                            logger.info(f"LIVE RESTORE: Registered tool_call[{len(ordered_tool_ids_dict)-1}]: {tool_id}")
        
        ordered_tool_ids = list(ordered_tool_ids_dict.keys())
        
        # Collect ToolMessages in order with their indices
        tool_message_indices = []
        for i, msg in enumerate(messages):
            msg_type = type(msg).__name__
            if 'Tool' in msg_type:
                tool_message_indices.append(i)
        
        # Match ToolMessages to tool_call_ids by position (1:1 mapping)
        # Assumption: ToolMessages appear in same order as tool_calls in AIMessages
        if len(tool_message_indices) == len(ordered_tool_ids):
            for tm_idx, correct_id in zip(tool_message_indices, ordered_tool_ids):
                msg = messages[tm_idx]
                current_id = getattr(msg, 'tool_call_id', None)
                if current_id != correct_id:
                    logger.info(f"RESTORING tool_call_id for Message {tm_idx}: {current_id} → {correct_id} (positional match)")
                    msg.tool_call_id = correct_id
        else:
            logger.warning(f"LIVE RESTORE: Mismatch in counts - {len(tool_message_indices)} ToolMessages vs {len(ordered_tool_ids)} tool_calls")
        
        # DEBUG: Log ToolMessage IDs at the START of consolidation (after restoration)
        for i, msg in enumerate(messages):
            msg_type = type(msg).__name__
            if 'Tool' in msg_type:
                tool_call_id = getattr(msg, 'tool_call_id', 'NO_ID')
                try:
                    content = json.loads(getattr(msg, 'content', '{}'))
                    command = content.get('final_command', 'NO_COMMAND')[:50]
                except:
                    command = 'PARSE_ERROR'
                logger.info(f"CONSOLIDATION START: Message {i} is ToolMessage with tool_call_id={tool_call_id}, command={command}")
            
        consolidated_messages = []
        chunk_builders = {}
        agent_processed_ids = set()
        
        # Identify messages already processed by the agent
        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    tool_call_id = tool_call.get('id', '')
                    if tool_call_id and 'run-' in tool_call_id and '_' in tool_call_id:
                        agent_processed_ids.add(msg.id)
                        break
        
        # Process chunks
        for msg in messages:
            if isinstance(msg, AIMessageChunk):
                if msg.id in agent_processed_ids:
                    logger.debug(f"CONSOLIDATION: Skipping chunk for agent-processed message ID: {msg.id}")
                    continue
                
                # Initialize or get builder for this chunk
                builder = chunk_builders.get(msg.id)
                if builder is None:
                    builder = {
                        "content": "",
                        "additional_kwargs": msg.additional_kwargs.copy() if msg.additional_kwargs else {},
                        "response_metadata": msg.response_metadata.copy() if msg.response_metadata else {},
                        "tool_calls": msg.tool_calls.copy() if hasattr(msg, 'tool_calls') and msg.tool_calls else [],
                        "id": msg.id,
                        "usage_metadata": None,
                    }
                    chunk_builders[msg.id] = builder

                # Accumulate content (handles Gemini thinking model list format)
                msg_content = _extract_text_from_content(msg.content or "")
                builder["content"] += msg_content

                # Process tool calls
                self._process_tool_calls_for_chunk(msg, builder)

                # Keep the latest usage_metadata
                if getattr(msg, 'usage_metadata', None):
                    builder["usage_metadata"] = msg.usage_metadata

                # Check if this is a terminating chunk
                finish_reason = None
                if isinstance(msg.response_metadata, dict):
                    finish_reason = msg.response_metadata.get("finish_reason")

                # Finalize AIMessage if terminating
                if finish_reason in ("tool_calls", "stop"):
                    if builder["tool_calls"]:
                        logger.debug(f"CONSOLIDATION: Reconstructed {len(builder['tool_calls'])} tool calls for message {msg.id}")
                    
                    cleaned_tool_calls = self._clean_tool_calls(builder["tool_calls"])

                    # Tool call IDs: OpenAI uses "call_xxx", Gemini uses "tool_xxx", Claude uses "toolu_xxx"
                    # Only skip if ALL tool calls have malformed/empty IDs
                    has_valid_tool_calls = any(
                        str(tc.get('id', '')).startswith('call_') or 
                        str(tc.get('id', '')).startswith('tool_') or
                        str(tc.get('id', '')).startswith('toolu_')
                        for tc in cleaned_tool_calls
                    )
                    
                    # Create AIMessage if it has valid tool calls OR if it has content
                    if has_valid_tool_calls or builder["content"] or not cleaned_tool_calls:
                        ai_msg = self._create_ai_message_from_builder(builder, cleaned_tool_calls)
                        consolidated_messages.append(ai_msg)
                        logger.debug(f"CONSOLIDATION: Created AIMessage {msg.id} with {len(cleaned_tool_calls)} tool calls")
                    else: 
                        logger.warning(f"CONSOLIDATION: Skipping AIMessage {msg.id} - no valid tool calls or content")
                    del chunk_builders[msg.id]
            else:
                # Non-chunk messages can be added directly
                consolidated_messages.append(msg)
        
        # Handle any remaining unfinalized chunks
        for msg_id, builder in chunk_builders.items():
            logger.warning(f"Finalizing unfinished chunk builder for message {msg_id}")
            cleaned_tool_calls = self._clean_tool_calls(builder["tool_calls"])
            has_valid_tool_calls = any(str(tc.get('id', '')).startswith('call_') for tc in cleaned_tool_calls)
            if has_valid_tool_calls or builder["content"] or not cleaned_tool_calls:
                ai_msg = self._create_ai_message_from_builder(builder, cleaned_tool_calls)
                consolidated_messages.append(ai_msg)
            else:
                logger.warning(f"CONSOLIDATION: Skipping unfinished message {msg_id} - no valid tool calls or content")
        
        # Remove duplicates
        final_messages = self._deduplicate_messages(consolidated_messages)
        
        # DEBUG: Log all ToolMessage IDs after deduplication
        for i, msg in enumerate(final_messages):
            msg_type = type(msg).__name__
            if 'Tool' in msg_type:
                tool_call_id = getattr(msg, 'tool_call_id', 'NO_ID')
                try:
                    content = json.loads(getattr(msg, 'content', '{}'))
                    command = content.get('final_command', 'NO_COMMAND')[:50]
                except:
                    command = 'PARSE_ERROR'
                logger.info(f"AFTER DEDUP: Message {i} is ToolMessage with tool_call_id={tool_call_id}, command={command}")
        
        # Update the state and record placeholder warnings
        self._set_state_attr(self._last_state, 'messages', final_messages)
        has_placeholders = self._scan_for_placeholders(final_messages)
        self._set_state_attr(self._last_state, 'placeholder_warning', has_placeholders)
        if has_placeholders:
            logger.warning("Placeholder tokens detected in AI response. Prompt will reinforce tool usage next turn.")

        # Persist last tool failure metadata (if any) for prompt reinforcement
        last_failure = self._extract_last_tool_failure(final_messages)
        self._set_state_attr(self._last_state, 'last_tool_failure', last_failure)
        if last_failure:
            logger.warning(
                "Tool failure detected: %s - %s",
                last_failure.get('tool_name'),
                last_failure.get('message', '')[:120]
            )
        logger.info(f"Consolidated {len(messages)} messages into {len(final_messages)} messages")

    def _collect_remaining_tool_messages(self):
        """Collect any remaining ToolMessage instances from the ToolContextCapture
        (if present) and append them to the last state so they are persisted when
        the workflow is cancelled.
        This is a NO-OP when no tool capture instance exists or when there is no
        last_state available. It is safe to call multiple times.
        """
        tool_capture = getattr(self.agent, "tool_capture_instance", None)
        if tool_capture is None or self._last_state is None:
            return

        try:
            remaining_messages = tool_capture.get_collected_tool_messages()
        except Exception as e:
            logger.warning(f"Failed to collect remaining tool messages: {e}")
            remaining_messages = []

        if not remaining_messages:
            return

        logger.info(
            f"WORKFLOW: Appending {len(remaining_messages)} remaining tool messages before consolidation"
        )

        if hasattr(self._last_state, "get"):
            self._last_state["messages"].extend(remaining_messages)
        else:
            self._last_state.messages.extend(remaining_messages)

    def _convert_to_ui_messages(self, llm_messages, tool_capture=None):
        """Convert LLM messages to UI format for frontend display."""
        ui_messages = []
        tool_messages = []
        message_id = 1
        
        # First pass: Create UI messages for all AI messages
        for msg in llm_messages:
            msg_type = type(msg).__name__

            if 'System' in msg_type:
                continue
                
            if 'Human' in msg_type:
                content = str(getattr(msg, 'content', ''))
                # Do not include our special cancellation message in the UI
                if '[URGENT CANCELLATION]' in content:
                    continue
                    
                ui_messages.append({
                    'message_number': message_id,
                    'text': content,
                    'sender': 'user',
                    'isCompleted': True
                })
                message_id += 1
                
            elif 'AI' in msg_type:
                # Extract text content (handles Gemini thinking model list format)
                content = _extract_text_from_content(getattr(msg, 'content', ''))
                
                # Get the AIMessage's run_id for consistency (needed regardless of tool calls)
                run_id = getattr(msg, 'id', None)
                
                # Get tool calls (prioritize primary attribute)
                tool_calls = getattr(msg, 'tool_calls', [])
                if not tool_calls and hasattr(msg, 'additional_kwargs'):
                    tool_calls = msg.additional_kwargs.get('tool_calls', [])
                    logger.warning(f"UI CONVERSION: Using fallback tool_calls from additional_kwargs for message ID: {run_id}")

                ui_tool_calls = []
                msg_timestamp = None
                if hasattr(msg, "additional_kwargs") and isinstance(msg.additional_kwargs, dict):
                    msg_timestamp = msg.additional_kwargs.get("timestamp")
                
                if tool_calls:
                    logger.info(f"UI CONVERSION: Adding {len(tool_calls)} tool calls from AIMessage (run_id={run_id})")
                    for tool_call in tool_calls:
                        tool_name = tool_call.get('name') or (tool_call.get('function', {}).get('name'))
                        args_str = tool_call.get('args') or (tool_call.get('function', {}).get('arguments', '{}'))     
                        
                        # Use the consistent tool call ID that matches our agent processing
                        tool_call_id = tool_call.get('id')
                        logger.info(f"UI CONVERSION: Tool call {tool_name} with ID={tool_call_id} (parent AIMessage run_id={run_id})")
                        
                        try:
                            tool_args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        except json.JSONDecodeError:
                            tool_args = {'raw': args_str}

                        tool_call_timestamp = None
                        if isinstance(tool_call, dict):
                            tool_call_timestamp = tool_call.get("timestamp")
                        ui_tool_calls.append({
                            'id': tool_call_id,
                            'run_id': run_id,
                            'tool_name': tool_name,
                            'input': json.dumps(tool_args),
                            'output': None,
                            'status': 'running',
                            'timestamp': tool_call_timestamp or msg_timestamp or datetime.now().isoformat()
                        })
                
                ui_message = {
                    'message_number': message_id,
                    'run_id': run_id,
                    'text': content,
                    'sender': 'bot',
                    'isCompleted': True,
                    'timestamp': msg_timestamp or datetime.now().isoformat()
                }
                if ui_tool_calls:
                    ui_message['toolCalls'] = ui_tool_calls
                ui_messages.append(ui_message)
                message_id += 1
                
            elif 'Tool' in msg_type:
                tool_messages.append(msg)
                continue
        
        # Second pass: Process Tool messages to update existing bot messages
        logger.info(f"UI CONVERSION: Processing {len(tool_messages)} ToolMessages to associate with tool calls")
        
        ui_messages = self._associate_tool_calls_with_output(ui_messages, tool_messages)

        # Inject any pending RCA context updates into the closest tool call message.
        ui_messages = self._inject_rca_context_updates(ui_messages)

        #logger.info(f"UI CONVERSION: Converted llm_messages: {llm_messages} into ui_messages: {ui_messages}")
        #logger.info(f"Converted {len(llm_messages)} LLM messages to {len(ui_messages)} UI messages")
        return ui_messages

    def _inject_rca_context_updates(self, ui_messages: list) -> list:
        """Place correlated RCA context updates at the correct chronological position.
        
        Context updates arrive during an active RCA investigation. We track the LLM 
        message index when the update was injected, and use that to place the UI 
        message at the corresponding position in the conversation.
        """
        try:
            if not hasattr(self, "_last_state"):
                return ui_messages
            pending_updates = getattr(self, "_rca_ui_updates", None)
            if not pending_updates:
                if isinstance(self._last_state, dict):
                    pending_updates = self._last_state.get("rca_ui_updates")
                else:
                    pending_updates = getattr(self._last_state, "rca_ui_updates", None)
            if not pending_updates:
                return ui_messages

            for update in pending_updates:
                tool_call_id = update.get("tool_call_id")
                injected_at = update.get("injected_at")
                injection_index = update.get("injection_index")  # LLM message index when injected
                content = update.get("content", "")
                if not tool_call_id:
                    continue

                # Avoid duplicate injection
                already_present = False
                for msg in ui_messages:
                    for tc in msg.get("toolCalls", []) or []:
                        if tc.get("id") == tool_call_id:
                            already_present = True
                            break
                    if already_present:
                        break
                if already_present:
                    continue
                
                # Create a new bot message for the context update
                context_update_message = {
                    "message_number": len(ui_messages) + 1,
                    "text": "",
                    "sender": "bot",
                    "isCompleted": True,
                    "timestamp": injected_at,
                    "toolCalls": [{
                        "id": tool_call_id,
                        "run_id": None,
                        "tool_name": "rca_context_update",
                        "input": json.dumps({
                            "update_count": update.get("update_count", 1),
                            "source": update.get("source", "pagerduty"),
                            "injected_at": injected_at,
                        }),
                        "output": content,
                        "status": "completed",
                        "timestamp": injected_at or datetime.now().isoformat(),
                    }],
                }

                # Use injection_index to find the correct position
                # The injection_index is the LLM message count when the update was injected.
                # We need to map this to the UI message position.
                # UI messages roughly correspond to LLM messages, but with some differences:
                # - User message is always first
                # - Bot messages with tool calls come from AIMessage with tool_calls
                # - We skip ToolMessages in UI (they become output on the tool call)
                # 
                # A rough heuristic: place the update after UI message at index injection_index // 2
                # (since each tool interaction is typically AIMessage + ToolMessage = 2 LLM messages)
                # but maps to 1 UI message.
                
                if injection_index is not None:
                    # Map LLM message index to approximate UI message index
                    # Each tool call in UI typically corresponds to 2 LLM messages (AI + Tool)
                    # So divide by 2 to get approximate UI position
                    # Add 1 to account for the user message at the start
                    approx_ui_index = max(1, (injection_index // 2) + 1)
                    insert_index = min(approx_ui_index, len(ui_messages))
                    logger.info(f"UI CONVERSION: Using injection_index {injection_index} -> UI index {insert_index}")
                else:
                    # Fallback: insert after the last bot message with tool calls
                    insert_index = len(ui_messages)
                    for idx in range(len(ui_messages) - 1, -1, -1):
                        msg = ui_messages[idx]
                        if msg.get("sender") == "bot" and msg.get("toolCalls"):
                            insert_index = idx + 1
                            break
                
                ui_messages.insert(insert_index, context_update_message)
                logger.info(f"UI CONVERSION: Inserted RCA context update at index {insert_index}")

            # Renumber all messages after insertion
            for idx, msg in enumerate(ui_messages):
                msg["message_number"] = idx + 1

        except Exception as exc:
            logger.info("UI CONVERSION: Failed to inject RCA context updates: %s", exc)
        return ui_messages
    
    def _save_ui_messages(self, session_id: str, user_id: str, ui_messages: list, ui_state: Optional[dict] = None) -> bool:
        """Save UI-formatted messages and UI state to the database."""
        try:
            from utils.db.connection_pool import db_pool
            from datetime import datetime
            
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Update the messages field (UI format) and ui_state in the existing session
                if ui_state is not None:
                    cursor.execute("""
                        UPDATE chat_sessions 
                        SET messages = %s, ui_state = %s, updated_at = %s
                        WHERE id = %s AND user_id = %s
                    """, (json.dumps(ui_messages), json.dumps(ui_state), datetime.now(), session_id, user_id))
                else:
                    # Fallback to only updating messages if no UI state provided
                    cursor.execute("""
                        UPDATE chat_sessions 
                        SET messages = %s, updated_at = %s
                        WHERE id = %s AND user_id = %s
                    """, (json.dumps(ui_messages), datetime.now(), session_id, user_id))
                
                if cursor.rowcount > 0:
                    conn.commit()
                    logger.info(f"Updated UI messages{' and state' if ui_state else ''} for session {session_id}")
                    return True
                else:
                    # Session doesn't exist yet - this is expected for new sessions
                    # The frontend will create the session when it's ready
                    logger.info(f"Session {session_id} not found for UI message update - will be created by frontend")
                    return True  # Not an error condition
                    
        except Exception as e:
            logger.error(f"Error saving UI messages: {e}")
            return False

    def _associate_tool_calls_with_output(self, ui_messages, tool_messages):
        """Associate tool calls with output in the UI messages."""

        for msg in tool_messages:
            tool_call_id = getattr(msg, 'tool_call_id', None)
            
            # Try to parse ToolMessage content as JSON, but handle invalid JSON gracefully
            try:
                tool_content = json.loads(getattr(msg, 'content', '{}'))
                # Handle case where tool_content is a list (e.g., MCP tool responses like empty PR lists)
                if isinstance(tool_content, list):
                    command_matching = None
                else:
                    command_matching = tool_content.get('final_command')  # Optional - only for cloud_exec
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"UI CONVERSION: Failed to parse ToolMessage content as JSON: {e}. Using empty dict.")
                tool_content = {}
                command_matching = None
            
            logger.info(f"UI CONVERSION: Processing ToolMessage with tool_call_id={tool_call_id}, command='{command_matching}'")
            
            if not tool_call_id:
                logger.warning(f"UI CONVERSION: Skipping ToolMessage without tool_call_id")
                continue
                
            updated = False

            # Look through ALL UI messages and check if ANY tool call matches the tool_call_id
            # The tool_call_id is "call_xxx", NOT the AIMessage's run_id "run-xxx"
            for ui_msg in ui_messages:
                logger.debug(f"UI CONVERSION: Processing ui_msg: {ui_msg}")
                if ui_msg.get('sender') == 'bot' and ui_msg.get('toolCalls'):
                    ui_msg_tool_calls = ui_msg.get('toolCalls', [])
                    
                    # Find the specific tool call by matching the tool_call_id
                    for tool_call in ui_msg_tool_calls:
                        # CRITICAL: Match tool_call['id'] (call_xxx) with tool_call_id from ToolMessage
                        if tool_call.get('id') == tool_call_id:
                            # Optional: Double-check by matching commands if available (only for cloud_exec)
                            command_from_ui = None
                            if command_matching:  # Only validate command if it exists
                                try:
                                    ui_input = json.loads(tool_call.get('input', '{}'))
                                    command_from_ui = ui_input.get('command')
                                except (json.JSONDecodeError, AttributeError):
                                    logger.warning(f"UI CONVERSION: Could not parse command from tool message content: {getattr(msg, 'content', '')}")
                                    pass # Will still update if IDs match
                                
                                logger.debug(f"UI CONVERSION DEBUG: Matched tool_call_id {tool_call_id}, comparing commands ->\nCommand matching: {command_matching}\nUI command:   {command_from_ui}")
                                
                                # Validate command match if both are present
                                if command_from_ui and command_matching not in command_from_ui and command_from_ui not in command_matching:
                                    logger.warning(f"UI CONVERSION: Command mismatch for {tool_call_id}, skipping")
                                    continue
                            
                            # Update the tool call with output (ID match is sufficient)
                            tool_call['output'] = str(getattr(msg, 'content', ''))
                            tool_call['status'] = 'completed'
                            tool_call['timestamp'] = datetime.now().isoformat()  # Update timestamp to completion time
                            updated = True
                            logger.info(f"UI CONVERSION:Updated tool call {tool_call_id} (tool={tool_call.get('tool_name')}) in UI message {ui_msg['message_number']}")
                            break # Found and updated, stop searching this ui_msg
                    
                    if updated:
                        break
            
            if not updated:
                logger.warning(f"UI CONVERSION:Could not find matching tool call for ID: {tool_call_id}")

        return ui_messages
        

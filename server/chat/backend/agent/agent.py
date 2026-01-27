from langchain_core.messages import ToolMessage, AIMessage, HumanMessage
import logging
import os
from chat.backend.agent.db import PostgreSQLClient
from chat.backend.agent.llm import LLMManager
from chat.backend.agent.weaviate_client import WeaviateClient
from chat.backend.agent.utils.state import State
from chat.backend.agent.utils.tool_context_capture import ToolContextCapture
from langchain_openai import ChatOpenAI
from .tools.cloud_tools import set_websocket_context
from chat.backend.agent.utils.prefix_cache import PrefixCacheManager
from chat.backend.agent.prompt.prompt_builder import build_prompt_segments, assemble_system_prompt, register_prompt_cache_breakpoints
from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker, LLMUsage
import time
import asyncio

class Agent:
    def __init__(self, weaviate_client: WeaviateClient, postgres_client: PostgreSQLClient, websocket_sender=None, event_loop=None, ctx_len=10):
        self.llm_manager = LLMManager()
        self.postgres_client = postgres_client
        self.weaviate_client = weaviate_client
        self.ctx_len = ctx_len
        self.websocket_sender = websocket_sender  # Store websocket_sender directly
        self.event_loop = event_loop  # Store event loop for thread-safe async calls

    def set_tool_capture(self, tool_capture):
        """Set the tool capture instance to be used by this agent."""
        self.tool_capture_instance = tool_capture
        logging.info(f"Set tool capture instance for agent")

    def update_websocket_sender(self, websocket_sender, event_loop=None):
        """Update the websocket_sender reference for this agent."""
        if not websocket_sender or not event_loop:
            logging.warning("WEBSOCKET DEBUG: Websocket sender  or event loop is None - not updating")
            return
        self.websocket_sender = websocket_sender
        self.event_loop = event_loop
        
        logging.debug(f"WEBSOCKET DEBUG: Updated Agent websocket_sender to {self.websocket_sender}")
    def _cleanup_terraform_files(self, user_id: str = None, session_id: str = None):
        """Clean up terraform files to prevent conflicts and reduce context size."""
        try:
            from chat.backend.agent.tools.iac.iac_write_tool import get_terraform_directory
            
            # Get the terraform directory for this user/session
            if user_id and session_id:
                terraform_dir = get_terraform_directory(user_id, session_id)
            elif user_id:
                terraform_dir = get_terraform_directory(user_id)
            else:
                terraform_dir = get_terraform_directory()
            
            if terraform_dir.exists():
                import shutil
                # Clean up terraform state and cache
                terraform_folder = terraform_dir / ".terraform"
                if terraform_folder.exists():
                    shutil.rmtree(terraform_folder)
                    logging.info(f"Cleaned up .terraform folder at {terraform_folder}")
                
                # Clean up terraform plan files
                for plan_file in terraform_dir.glob("*.tfplan"):
                    plan_file.unlink()
                    logging.info(f"Cleaned up plan file: {plan_file}")
                
                # Clean up terraform lock files
                lock_file = terraform_dir / ".terraform.lock.hcl"
                if lock_file.exists():
                    lock_file.unlink()
                    logging.info(f"Cleaned up lock file: {lock_file}")
                    
                logging.info(f"Successfully cleaned up terraform files for user {user_id}, session {session_id}")
            else:
                logging.info(f"No terraform directory found to clean up for user {user_id}, session {session_id}")
                
        except Exception as e:
            logging.warning(f"Error during terraform cleanup: {e}")
            # Don't raise - cleanup failures shouldn't break the main flow


    # ---------------------------------------------------------------------
    # Agentic multi-tool workflow (cloud orchestration)
    # ---------------------------------------------------------------------
    def _prompt_references_zip(self, prompt: str, attachments: list) -> bool:
        """Return True if the prompt references a known zip file by name or generic keywords."""
        if not prompt:
            return False
        prompt_lower = prompt.lower()
        # Check for generic zip references
        if 'zip' in prompt_lower or 'archive' in prompt_lower:
            return True
        # Check for specific filenames
        if attachments:
            for att in attachments:
                fname = att.get('filename', '').lower()
                if fname and fname in prompt_lower:
                    return True
        return False

    def _get_github_username_for_user(self, user_id: str) -> str:
        """Get GitHub username for a specific user from database."""
        try:
            from utils.auth.stateless_auth import get_credentials_from_db
            github_creds = get_credentials_from_db(user_id, 'github')
            if github_creds and 'username' in github_creds:
                return github_creds['username']
        except Exception as e:
            logging.warning(f"Failed to get GitHub username for user {user_id}: {e}")
        return "YOUR_GITHUB_USERNAME"  # Fallback

    # ------------------------------------------------------------------
    # Helper methods extracted for better readability of agentic flow
    # ------------------------------------------------------------------

    def _log_attachments(self, state: "State") -> None:
        """Log any attachments present in the state for easier debugging."""
        attachments = getattr(state, 'attachments', None)
        if attachments:
            logging.info(f"agentic_tool_flow: State has {len(attachments)} attachments")
            for i, attachment in enumerate(attachments):
                filename = attachment.get('filename', 'unknown')
                is_server_path = attachment.get('is_server_path', False)
                logging.info(f"  Attachment {i}: {filename} (server_path: {is_server_path})")
        else:
            logging.info("agentic_tool_flow: State has no attachments")

    def _fetch_session_files(self, state: "State") -> None:
        """Populate `state.session_files` with any terraform_dir files stored in object storage."""
        try:
            user_id = getattr(state, 'user_id', None)
            session_id = getattr(state, 'session_id', None)
            if user_id and session_id:
                from utils.storage.storage import get_storage_manager
                storage_manager = get_storage_manager(user_id)
                prefix = f"{session_id}/terraform_dir/"
                import time as _time
                _start_ms = _time.perf_counter()
                # Decide if we should fetch storage files now to avoid unnecessary latency
                question_text = getattr(state, 'question', '') or ''
                attachments = getattr(state, 'attachments', [])
                should_fetch_storage = bool(attachments) or self._prompt_references_zip(question_text, attachments) or bool(getattr(state, 'deployment', False))
                if should_fetch_storage:
                    files = storage_manager.list_user_files(user_id, prefix=prefix, max_results=50)
                    _elapsed_ms = (_time.perf_counter() - _start_ms) * 1000.0
                    state.session_files = files
                    logging.info(f"Fetched {len(files)} files from storage for user {user_id}, session {session_id} (took {_elapsed_ms:.1f} ms; max_results=50)")
                else:
                    _elapsed_ms = (_time.perf_counter() - _start_ms) * 1000.0
                    logging.info(f"Skipping storage file fetch (no attachments/zip refs/not deploying) for user {user_id}, session {session_id} (decision took {_elapsed_ms:.1f} ms)")
            else:
                logging.info("Skipping storage file fetch: user_id or session_id missing")
        except Exception as e:
            logging.error(f"Error fetching session files from storage: {e}")

    async def agentic_tool_flow(self, state: State) -> State:
        """Execute cloud tools using the agentic workflow with streaming callbacks."""
        
        # ------------------------------------------------------------------
        # 1. Pre-run enrichment & quick logging
        # ------------------------------------------------------------------
        self._log_attachments(state)
        self._fetch_session_files(state)
        
        try:
            # Direct imports for LangChain 1.2.6+ - no fallbacks
            from langchain.agents import create_agent
            from langchain_core.callbacks import BaseCallbackHandler
            from .tools.cloud_tools import get_cloud_tools, set_user_context, set_tool_capture

            logging.info(f"agentic_tool_flow: State has user_id {state.user_id} and session_id {state.session_id}")
            # Set user context for tools
            if state.user_id:
                # Validate user_id format
                from utils.auth.stateless_auth import is_valid_user_id
                if not is_valid_user_id(state.user_id):
                    logging.error(f"Invalid user_id format: '{state.user_id}' - must be a non-empty string")
                    # Don't fail completely, but log the error
                
                # Get connected providers from database (always fetch from DB, no preferences stored)
                provider_preference = getattr(state, 'provider_preference', None)
                if provider_preference is None:
                    try:
                        from utils.auth.stateless_auth import get_connected_providers
                        provider_preference = get_connected_providers(state.user_id)
                    except Exception as e:
                        logging.error(f"Error getting connected providers: {e}")
                        provider_preference = []
                    # Update state so downstream code can access it
                    state.provider_preference = provider_preference
                
                selected_project_id = getattr(state, 'selected_project_id', None)
                
                # Log connected providers for debugging
                if provider_preference:
                    logging.info(f"Using connected providers from database: {provider_preference} (type: {type(provider_preference)})")
                else:
                    logging.warning("No connected providers found in database")
                
                set_user_context(
                    user_id=state.user_id,
                    session_id=state.session_id,
                    provider_preference=provider_preference,
                    selected_project_id=selected_project_id,
                    state=state,
                    mode=getattr(state, 'mode', None),
                )
                logging.info(f"Set user context for tools: {state.user_id}, session: {state.session_id}, provider: {provider_preference}, project: {selected_project_id}")
            else:
                logging.warning("No user_id in state - tools may fail")

            # Use shared tool_capture instance from workflow if available, otherwise create new one
            tool_capture = getattr(self, 'tool_capture_instance', None)
            if not tool_capture and state.session_id and state.user_id:
                tool_capture = ToolContextCapture(state.session_id, state.user_id)
                logging.debug(f"Created new tool capture for session {state.session_id}")                 
            elif tool_capture:
                logging.debug(f"Using shared tool_capture instance from workflow")
            
            # Always set tool capture in thread-local context so tools can access it
            if tool_capture:
                set_tool_capture(tool_capture)
                logging.debug(f"Set tool capture in thread-local context for session {state.session_id}")

            # Set WebSocket context for tools so they can send completion notifications
            if self.websocket_sender and self.event_loop:
                set_websocket_context(self.websocket_sender, self.event_loop)
                logging.debug("Set WebSocket context for tool completion notifications")
            else:
                logging.warning("No WebSocket sender available - tools won't send completion notifications")

        
            # Build system prompt as ChatPromptTemplate
            # Get the provider preference from the state
            provider_preference = state.provider_preference
            logging.info(f"this bot is using {provider_preference}")
            
            # Determine if long-doc zip is referenced in the prompt for a short note segment
            has_zip_ref = self._prompt_references_zip(
                prompt_text if 'prompt_text' in locals() else getattr(state, 'question', '') or '',
                getattr(state, 'attachments', []),
            )

            # Build modular segments
            segments = build_prompt_segments(
                provider_preference=provider_preference,
                mode=getattr(state, "mode", None),
                has_zip_reference=has_zip_ref,
                state=state,
            )

            # Assemble final system prompt from segments
            system_prompt_text = assemble_system_prompt(segments)
            
            # Get cloud tools
            tools = get_cloud_tools()
            
            
            prompt_text = ''
            if state.messages and hasattr(state.messages[-1], 'content'):
                # Handle both string and multimodal content
                last_content = state.messages[-1].content
                if isinstance(last_content, str):
                    prompt_text = last_content
                elif isinstance(last_content, list):
                    # Extract text parts
                    prompt_text = ' '.join([p['text'] if isinstance(p, dict) and p.get('type') == 'text' else str(p) for p in last_content])
            # Only include zip-related tools if referenced
            if not self._prompt_references_zip(prompt_text, getattr(state, 'attachments', [])):
                tools = [t for t in tools if getattr(t, 'name', None) not in ('analyze_zip_file', 'rag_index_zip')]

            # Register canonicalized prefix + tools with cache middleware
            try:
                pcm = PrefixCacheManager.get_instance()
                provider = None
                # Determine provider from state/provider_preference
                pref = getattr(state, 'provider_preference', None)
                if isinstance(pref, list) and pref:
                    provider = pref[0]
                elif isinstance(pref, str):
                    provider = pref
                
                # Only register cache if provider is set
                if provider:
                    provider = provider.lower()
                    tenant_id = getattr(state, 'user_id', None) or "public"
                    # Register segmented cache breakpoints: tools, system_invariant, provider_constraints, regional_rules, ephemeral
                    register_prompt_cache_breakpoints(
                        pcm=pcm,
                        segments=segments,
                        tools=tools,
                        provider=provider,
                        tenant_id=tenant_id,
                    )
                    try:
                        from chat.backend.agent.utils.telemetry import emit_vendor_cache_event
                        emit_vendor_cache_event(provider, "segments_registered", {"has_ephemeral": bool(segments.ephemeral_rules)})
                    except Exception:
                        pass
            except Exception as e:
                logging.debug(f"Prefix cache registration failed: {e}")
            # Check if state contains multimodal content to determine model
            has_images = False
            try:
                for message in state.messages:
                    if hasattr(message, 'content') and isinstance(message.content, list):
                        for content_part in message.content:
                            if isinstance(content_part, dict) and content_part.get('type') == 'image_url':
                                has_images = True
                                break
                    if has_images:
                        break
            except Exception as e:
                logging.warning(f"Error checking for multimodal content: {e}")
                
            # Determine which model to use based on user selection and content type
            if state.model:
                # Use selected model from frontend
                model_name = state.model
                logging.info(f"Using user-selected model for agentic workflow: {model_name}")
            elif has_images:
                # Fall back to vision model for images if no model selected
                model_name = "openai/gpt-4o"  # Vision-capable model
                logging.info("Using vision model for agentic workflow due to multimodal content")
            else:
                # Default main model
                model_name = "openai/gpt-4o"  # Default main model
                logging.info("Using default main model for agentic workflow")
            
            
            # Create a custom callback for tracking LLM usage
            class AgentLLMUsageCallback(BaseCallbackHandler):
                """Tracks LLM usage for agent workflows."""
                def __init__(self, user_id: str, session_id: str, model_name: str, api_provider: str):
                    self.user_id = user_id
                    self.session_id = session_id
                    self.model_name = model_name
                    self.api_provider = api_provider
                    self.current_calls = {}
                    
                def on_llm_start(self, serialized, prompts, run_id=None, **kwargs):
                    """Track LLM call start."""
                    try:
                        # Count input tokens
                        input_tokens = 0
                        for prompt in prompts:
                            input_tokens += LLMUsageTracker.count_tokens(str(prompt), self.model_name)
                        
                        if run_id:
                            self.current_calls[run_id] = {
                                'input_tokens': input_tokens,
                                'start_time': time.time()
                            }
                        logging.debug(f"Agent LLM Start: {self.model_name} - {input_tokens} input tokens")
                    except Exception as e:
                        logging.warning(f"Error tracking agent LLM start: {e}")
                
                def on_llm_end(self, response, run_id=None, **kwargs):
                    """Track LLM call end and store usage."""
                    try:
                        if not run_id or run_id not in self.current_calls:
                            return
                        
                        call_info = self.current_calls[run_id]
                        
                        # Count output tokens
                        output_tokens = 0
                        if hasattr(response, 'generations'):
                            for gen_list in response.generations:
                                for gen in gen_list:
                                    if hasattr(gen, 'text'):
                                        output_tokens += LLMUsageTracker.count_tokens(gen.text, self.model_name)
                        
                        # Calculate cost
                        estimated_cost = LLMUsageTracker.calculate_cost(
                            call_info['input_tokens'], output_tokens, self.model_name
                        )
                        
                        # Store usage
                        usage = LLMUsage(
                            user_id=self.user_id,
                            session_id=self.session_id,
                            model_name=self.model_name,
                            api_provider=self.api_provider,
                            request_type="agent_workflow",
                            input_tokens=call_info['input_tokens'],
                            output_tokens=output_tokens,
                            estimated_cost=estimated_cost,
                            response_time_ms=int((time.time() - call_info['start_time']) * 1000),
                            error_message=None,
                            request_metadata={'agent_executor': True}
                        )
                        
                        if LLMUsageTracker.store_usage(usage):
                            logging.info(f"Agent LLM Tracked: {self.model_name} - {call_info['input_tokens']}+{output_tokens} tokens - ${estimated_cost:.6f}")
                        else:
                            logging.warning("Failed to store agent LLM usage")
                        
                        del self.current_calls[run_id]
                    except Exception as e:
                        logging.warning(f"Error tracking agent LLM end: {e}")
            
            # Get provider mode from LLM manager
            provider_mode = self.llm_manager.provider_mode
            
            # Determine the actual provider for usage tracking
            from chat.backend.agent.model_mapper import ModelMapper
            detected_provider = ModelMapper.detect_provider(model_name) if provider_mode != "openrouter" else "openrouter"
            
            # Create the usage tracking callback with correct provider
            usage_callback = AgentLLMUsageCallback(
                user_id=state.user_id,
                session_id=state.session_id,
                model_name=model_name,
                api_provider=detected_provider
            )
            
            # Create streaming LLM based on provider mode
            if provider_mode == "openrouter":
                # Use OpenRouter mode
                openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
                if not openrouter_api_key:
                    raise ValueError("OPENROUTER_API_KEY environment variable is not set")

                # Convert model name to OpenRouter format (e.g., claude-sonnet-4-5 -> claude-sonnet-4.5)
                from chat.backend.agent.model_mapper import ModelMapper
                openrouter_model_name = ModelMapper.get_native_name(model_name, "openrouter")

                # Create ChatOpenAI with callbacks attached directly to the LLM
                streaming_llm = ChatOpenAI(
                    model=openrouter_model_name,
                    temperature=self.llm_manager.main_llm.temperature,
                    streaming=True,  # Enable streaming
                    openai_api_base="https://openrouter.ai/api/v1",
                    openai_api_key=openrouter_api_key,
                    callbacks=[usage_callback],  # CRITICAL FIX: Attach the usage tracking callback!
                    request_timeout=120.0,  # Increase timeout to 2 minutes
                    max_retries=3  # Add retries for network stability
                )
            else:
                # Use direct/auto mode - use provider system
                from chat.backend.agent.providers import create_chat_model
                
                # Create streaming LLM using provider system
                # Note: Providers handle their own timeout/retry defaults
                # (OpenAI uses request_timeout, Anthropic uses timeout, etc.)
                streaming_llm = create_chat_model(
                    model=model_name,
                    temperature=self.llm_manager.main_llm.temperature,
                    provider_mode=provider_mode,
                    streaming=True,  # Enable streaming
                    callbacks=[usage_callback],  # Attach the usage tracking callback
                )
            
            # Create the agent using new LangChain 1.2.6+ API
            agent_graph = create_agent(
                model=streaming_llm,
                tools=tools,
                system_prompt=system_prompt_text
            )

      
            try:         
                # Get recursion limit from environment variable (required)
                max_iterations = int(os.environ["AGENT_RECURSION_LIMIT"])
                
                # Prepare chat history for the agent - handle LangChain message objects
                chat_history = []
                
                # Use only the last self.ctx_len messages from history (exclude the current user message)
                prev_messages = state.messages[:-1]
                if len(prev_messages) > self.ctx_len:
                    prev_messages = prev_messages[-self.ctx_len:]

                for msg in prev_messages:  # Exclude the current user message
                    if isinstance(msg, HumanMessage):
                        # For multimodal messages, we need to pass the full content structure
                        chat_history.append(("human", msg.content))
                    elif isinstance(msg, AIMessage):
                        # Handle AIMessage with tool calls
                        if hasattr(msg, 'tool_calls') and msg.tool_calls:
                            ai_content = msg.content
                            if not ai_content:
                                # Build a compact description of the tool calls for context without noisy placeholders
                                tool_summaries = []
                                for call in msg.tool_calls:
                                    name = call.get('name') if isinstance(call, dict) else getattr(call, 'name', 'tool')
                                    args = call.get('args') if isinstance(call, dict) else getattr(call, 'args', {})
                                    tool_summaries.append(f"[{name}] {args}")
                                ai_content = " \n".join(tool_summaries)
                            if ai_content:
                                chat_history.append(("ai", ai_content))
                        else:
                            if msg.content:
                                chat_history.append(("ai", msg.content))
                    elif isinstance(msg, ToolMessage):
                        # Handle ToolMessage objects - use summarized content if available to reduce tokens
                        tool_name = getattr(msg, 'name', 'unknown_tool')
                        tool_result = msg.content
                        
                        # Check if we have a summarized version for this tool message
                        if (tool_capture and 
                            hasattr(tool_capture, 'summarized_tool_results') and 
                            hasattr(msg, 'tool_call_id') and 
                            msg.tool_call_id in tool_capture.summarized_tool_results):
                            # Use summarized content for chat history to reduce tokens
                            summarized_data = tool_capture.summarized_tool_results[msg.tool_call_id]
                            tool_result = summarized_data['summarized_output']
                            logging.info(f"Using summarized tool result for {tool_name} in chat history")
                        else:
                            # Truncate very large tool outputs to avoid token bloat
                            if isinstance(tool_result, str) and len(tool_result) > 4000:
                                tool_result = tool_result[:4000] + "\n...[truncated for context reduction]"
                        
                        tool_context = f"[Tool: {tool_name}] {tool_result}"
                        chat_history.append(("system", tool_context))
                    elif isinstance(msg, dict):
                        # Fallback for dict-style messages
                        if msg.get("role") == "user":
                            chat_history.append(("human", msg.get("content", "")))
                        elif msg.get("role") == "assistant":
                            chat_history.append(("ai", msg.get("content", "")))
                        elif msg.get("role") == "tool":
                            # Handle tool result dict messages
                            tool_name = msg.get("name", "unknown_tool")
                            tool_result = msg.get("content", "")
                            tool_context = f"[Tool: {tool_name}] {tool_result}"
                            chat_history.append(("system", tool_context))
                    else:
                        # Unknown type fallback
                        chat_history.append(("system", getattr(msg, 'content', "")))


                # Preflight context compression for LLM prompt only (does not alter stored messages)
                if prev_messages:
                    try:
                        from chat.backend.agent.utils.chat_context_manager import ChatContextManager
                        if ChatContextManager.should_summarize_context(prev_messages, model_name):
                            summary_text = ChatContextManager.create_conversation_summary(prev_messages)
                            chat_history = [(
                                "system",
                                "[CONVERSATION SUMMARY - Preflight]\n\n"
                                f"{summary_text}\n\n"
                                "[END SUMMARY]"
                            )]
                            logging.info(f"Preflight context compression applied for session {state.session_id}")
                    except Exception as e:
                        logging.warning(f"Preflight context compression failed: {e}")

                # Execute the agent - get current query from last message
                # For multimodal messages, we need to preserve the entire structure
                if isinstance(state.messages[-1], HumanMessage):
                    current_query = state.messages[-1].content
                    # If content is a list (multimodal), extract just the text for the query
                    # but keep the full message structure in chat_history
                    if isinstance(current_query, list):
                        # Extract text parts for the query string
                        text_parts = []
                        for part in current_query:
                            if isinstance(part, dict) and part.get('type') == 'text':
                                text_parts.append(part.get('text', ''))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        current_query = ' '.join(text_parts)
                        logging.info(f"Extracted text from multimodal message: {current_query}")
                elif isinstance(state.messages[-1], dict):
                    current_query = state.messages[-1].get("content", "")
                else:
                    current_query = str(state.messages[-1])
                    
                # Inject conversation context to help agent remember original goals
                context_injection = ""
                if len(chat_history) > 0:
                    # Find the original request (first human message)
                    original_request = None
                    for role, content in chat_history:
                        if role == "human":
                            original_request = content
                            break
                    
                    if original_request and original_request != current_query:
                        context_injection = f"\n\nCONTEXT REMINDER: The original request in this conversation was: '{original_request}'. "
                        context_injection += "If the current message relates to handling errors or changing approaches for that original task, "
                        context_injection += "make sure to apply the same original goal/requirements in the new context."
                        current_query += context_injection
                
                # Build messages list for new create_agent API
                # The new API expects a list of messages, not a dict
                from langchain_core.messages import SystemMessage
                agent_messages = []
                
                # Add chat history messages
                for msg in chat_history:
                    if isinstance(msg, tuple):
                        role, content = msg
                        if role == "human":
                            agent_messages.append(HumanMessage(content=content))
                        elif role == "ai":
                            agent_messages.append(AIMessage(content=content))
                        elif role == "system":
                            agent_messages.append(SystemMessage(content=content))
                    else:
                        # Already a message object
                        agent_messages.append(msg)
                
                # Add current query as HumanMessage
                last_message = state.messages[-1]
                if isinstance(last_message, HumanMessage) and isinstance(last_message.content, list):
                    # Multimodal message - use directly
                    agent_messages.append(HumanMessage(content=last_message.content))
                else:
                    agent_messages.append(HumanMessage(content=current_query))
                
                # Execute the agent workflow using new API with streaming
                try:
                    # Retry logic for network errors
                    for attempt in range(3):
                        try:
                            # Use astream_events for token-by-token streaming
                            logging.info(f"Starting agent token streaming for session {state.session_id}")
                            result = None
                            event_count = 0
                            token_count = 0
                            event_types_seen = set()
                            
                            async for event in agent_graph.astream_events(
                                {"messages": agent_messages},
                                config={"recursion_limit": max_iterations},
                                version="v2"
                            ):
                                event_count += 1
                                event_type = event.get("event")
                                event_name = event.get("name", "")
                                event_types_seen.add(event_type)
                                
                                # Debug: log first 10 events to see what we're getting
                                if event_count <= 10:
                                    logging.info(f"Event #{event_count}: type={event_type}, name={event_name}")
                                
                                # Track tokens (streaming is handled by workflow.py -> main_chatbot.py)
                                if event_type == "on_chat_model_stream":
                                    chunk_data = event.get("data", {})
                                    chunk_obj = chunk_data.get("chunk")
                                    if chunk_obj and hasattr(chunk_obj, 'content') and chunk_obj.content:
                                        token_count += 1
                                        # Log first few tokens for debugging
                                        if token_count <= 5:
                                            logging.info(f"Streaming token #{token_count}: '{chunk_obj.content}'")

                                # Handle tool call events
                                elif event_type == "on_chat_model_end":
                                    # Check for tool calls in the final message
                                    chunk_data = event.get("data", {})
                                    output = chunk_data.get("output")
                                    if output and hasattr(output, 'tool_calls') and output.tool_calls:
                                        logging.debug(f"Detected {len(output.tool_calls)} tool calls at model end")
                                
                                # Capture final state from chain end events
                                elif event_type == "on_chain_end" and event_name == "LangGraph":
                                    # This is the final result from the agent graph
                                    output_data = event.get("data", {}).get("output")
                                    if output_data:
                                        result = output_data
                                        logging.debug(f"Captured final agent result at chain end")
                            
                            logging.info(f"Agent streaming completed for session {state.session_id} - received {event_count} events, {token_count} tokens")
                            logging.info(f"Event types seen: {sorted(event_types_seen)}")
                            break # Break retry loop on success
                        except Exception as e:
                            error_str = str(e)
                            error_type = type(e).__name__
                            # Check for network/protocol errors that should be retried
                            is_network_error = any(kw in error_str for kw in [
                                "ReadError", 
                                "ConnectError", 
                                "Timeout",
                                "RemoteProtocolError",
                                "incomplete chunked read",
                                "peer closed connection"
                            ]) or "RemoteProtocolError" in error_type
                            if attempt < 2 and is_network_error:
                                logging.warning(f"Network error (attempt {attempt+1}/3): {e}. Retrying...")
                                await asyncio.sleep(2 * (attempt + 1))
                            else:
                                raise
                                
                except Exception as e:
                    logging.error(f"Agent execution failed after retries: {e}", exc_info=True)
                    # Create error response message
                    error_msg = AIMessage(content=f"Error: {str(e)}\n\nTry a different approach.")
                    state.messages.append(error_msg)
                    return state
                
                # Process result from streaming API - result contains messages from final values event
                # The new create_agent API returns a dict with 'messages' key
                # Messages are in order: input messages + new AI responses + tool calls + tool results + final response
                if result and 'messages' in result:
                    new_messages = result['messages']
                    # Filter out messages that were already in our input (agent_messages)
                    # Use message IDs to avoid duplicates, as message objects might not compare correctly
                    input_message_ids = {id(msg) for msg in agent_messages}
                    
                    for msg in new_messages:
                        # Only append messages that weren't in our input
                        if id(msg) not in input_message_ids:
                            state.messages.append(msg)
                elif result:
                    # Fallback: try to extract final message (shouldn't happen with new API)
                    logging.warning(f"Unexpected result format from create_agent: {type(result)}")
                    final_content = result.get('output', str(result))
                    final_ai_message = AIMessage(content=final_content)
                    state.messages.append(final_ai_message)
                else:
                    logging.warning("No final result received from agent streaming - messages may have been streamed directly")
                
                return state
                
            finally:   
                # ALWAYS cleanup terraform files after any deployment workflow (success or failure)
                # This prevents SSH key errors, conflicts, and reduces context size in future runs
                if isinstance(state.messages[-1], (HumanMessage, dict)):
                    last_msg_content = state.messages[-1].content if isinstance(state.messages[-1], HumanMessage) else state.messages[-1].get("content", "")
                else:
                    last_msg_content = str(state.messages[-1])
                    
                # Check if this was a deployment-related workflow
                if "deploy" in last_msg_content.lower() or "terraform" in last_msg_content.lower():
                    try:
                        self._cleanup_terraform_files(state.user_id, state.session_id)
                        
                    except Exception as cleanup_error:
                        logging.warning(f"Failed to cleanup terraform files: {cleanup_error}")
                        # Don't let cleanup errors break the flow
            
        except Exception as e:
            import traceback
            logging.error(f"Error in agentic_tool_flow: {e}")
            logging.error(f"Full traceback:\n{traceback.format_exc()}")
            
            # Still attempt cleanup on error
            try:
                self._cleanup_terraform_files(state.user_id if hasattr(state, 'user_id') else None)
                
            except:
                pass  # Ignore cleanup errors in error handler
                
            # Fallback response
            state.messages.append(AIMessage(content=f"I encountered an error while processing your cloud management request: {str(e)}"))
            return state

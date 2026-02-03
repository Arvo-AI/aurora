"""
Chat Context Manager for Aurora Chat

Handles automatic context summarization to prevent exceeding model context limits.
Monitors token usage and summarizes conversation history when approaching limits.
"""

import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
import tiktoken

from .llm_context_manager import LLMContextManager
from ..llm import LLMManager
from .llm_usage_tracker import LLMUsageTracker
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


class ChatContextManager:
    """Manages chat context length to prevent exceeding model limits."""

    # Model context limits (tokens) - leaving some buffer for system prompts and tool calls
    MODEL_CONTEXT_LIMITS = {
        "openai/gpt-5.2": 950000,  # 1M - 50K buffer
        "anthropic/claude-sonnet-4.5": 950000,  # 1M - 50K buffer
        "anthropic/claude-opus-4.5": 180000,  # 200K - 20K buffer
        "anthropic/claude-3-haiku": 180000,  # 200K - 20K buffer
        "anthropic/claude-3.5-sonnet": 180000,  # 200K - 20K buffer
        "google/gemini-3-pro-preview": 1000000,  # 1M context
        # Default fallback
        "default": 7000,  # Conservative 8K - 1K buffer
    }

    @classmethod
    def get_context_limit(cls, model_name: str) -> int:
        """Get the context limit for a specific model."""
        # Try exact match first
        if model_name in cls.MODEL_CONTEXT_LIMITS:
            return cls.MODEL_CONTEXT_LIMITS[model_name]

        # Try without version suffix
        base_model = model_name.split(".")[0].split("-v")[0]
        if base_model in cls.MODEL_CONTEXT_LIMITS:
            return cls.MODEL_CONTEXT_LIMITS[base_model]

        # Use default
        logger.warning(f"Unknown model {model_name}, using default context limit")
        return cls.MODEL_CONTEXT_LIMITS["default"]

    @classmethod
    def count_tokens_in_messages(
        cls, messages: List[Any], model_name: str = "gpt-4"
    ) -> int:
        """Count total tokens in a list of messages."""
        return LLMUsageTracker.count_tokens_from_messages(messages, model_name)

    @classmethod
    def should_summarize_context(cls, messages: List[Any], model_name: str) -> bool:
        """Check if context should be summarized based on current token count and model limits."""
        current_tokens = cls.count_tokens_in_messages(messages, model_name)
        context_limit = cls.get_context_limit(model_name)

        # Trigger summarization at 80% of context limit
        threshold = int(context_limit * 0.80)

        logger.info(
            f"Context check: {current_tokens}/{context_limit} tokens ({(current_tokens / context_limit) * 100:.1f}%)"
        )

        return current_tokens > threshold

    @classmethod
    def create_conversation_summary(
        cls, messages: List[Any]
    ) -> str:
        """Create a summary of the conversation history using centralized model config."""
        try:
            # Convert messages to a readable format for summarization
            conversation_text = cls._format_messages_for_summary(messages)

            logger.info(
                f"Creating conversation summary from {len(messages)} messages ({len(conversation_text)} chars)"
            )

            # Create summarization prompt
            summarization_prompt = f"""Please provide a comprehensive summary of this chat conversation between a user and an AI assistant.

IMPORTANT: Preserve all key information including:
- User's main goals and requests throughout the conversation
- Important decisions made and resources created/modified
- Technical details like resource names, regions, project IDs
- Any ongoing tasks or issues that need continuation
- Context that would be important for future responses

The summary should be detailed enough that the AI assistant can continue helping the user effectively based on this context.

Conversation to summarize:
{conversation_text}


Provide a detailed summary that preserves essential context:"""

            # Create isolated LLM instance for summarization
            from ..llm import ModelConfig
            llm_manager = LLMManager()
            summary = llm_manager.summarize(
                conversation_text, model=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL
            )

            logger.info(f"Generated conversation summary ({len(summary)} chars)")
            return summary

        except Exception as e:
            logger.error(f"Error creating conversation summary: {e}")
            # Fallback to simple truncation
            fallback_summary = cls._create_fallback_summary(messages)
            return fallback_summary

    @classmethod
    def _format_messages_for_summary(cls, messages: List[Any]) -> str:
        """Format messages into readable text for summarization."""
        formatted_parts = []

        for i, msg in enumerate(messages):
            try:
                if isinstance(msg, HumanMessage):
                    content = cls._extract_message_content(msg)
                    formatted_parts.append(f"User: {content}")

                elif isinstance(msg, AIMessage):
                    content = cls._extract_message_content(msg)
                    if content:  # Skip empty AI messages
                        formatted_parts.append(f"Assistant: {content}")

                elif isinstance(msg, ToolMessage):
                    # Include tool results but truncated
                    tool_name = getattr(msg, "name", "unknown_tool")
                    content = str(msg.content)[:500]  # Truncate tool outputs
                    if len(str(msg.content)) > 500:
                        content += "... [truncated]"
                    formatted_parts.append(f"[Tool: {tool_name}] {content}")

                elif isinstance(msg, dict):
                    # Handle dict-format messages
                    role = msg.get("role", "unknown")
                    content = str(msg.get("content", ""))
                    if role == "user":
                        formatted_parts.append(f"User: {content}")
                    elif role == "assistant":
                        formatted_parts.append(f"Assistant: {content}")

            except Exception as e:
                logger.warning(f"Error formatting message {i}: {e}")
                continue

        return "\n\n".join(formatted_parts)

    @classmethod
    def _extract_message_content(cls, msg: Any) -> str:
        """Extract content from a message, handling multimodal content."""
        if hasattr(msg, "content"):
            content = msg.content
            if isinstance(content, list):
                # Handle multimodal content
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            text_parts.append("[Image attached]")
                    elif isinstance(part, str):
                        text_parts.append(part)
                return " ".join(text_parts)
            else:
                return str(content)
        return ""

    @classmethod
    def _create_fallback_summary(cls, messages: List[Any]) -> str:
        """Create a simple fallback summary when full summarization fails."""
        try:
            # Extract key information from recent messages
            recent_messages = messages[-10:]  # Last 10 messages
            user_requests = []
            assistant_actions = []

            for msg in recent_messages:
                content = cls._extract_message_content(msg)
                if isinstance(msg, HumanMessage):
                    user_requests.append(content)
                elif isinstance(msg, AIMessage) and content:
                    assistant_actions.append(content[:200])  # Truncate

            summary_parts = ["=== CONVERSATION SUMMARY ==="]

            if user_requests:
                summary_parts.append("USER REQUESTS:")
                for req in user_requests[-5:]:  # Last 5 requests
                    summary_parts.append(f"- {req}")

            if assistant_actions:
                summary_parts.append("ASSISTANT ACTIONS:")
                for action in assistant_actions[-3:]:  # Last 3 actions
                    summary_parts.append(f"- {action}")

            summary_parts.append("=== END SUMMARY ===")

            return "\n".join(summary_parts)

        except Exception as e:
            logger.error(f"Error creating fallback summary: {e}")
            return "Previous conversation context was summarized due to length limits."

    @classmethod
    def compress_context_if_needed(
        cls,
        session_id: str,
        user_id: str,
        messages: List[Any],
        model_name: str,
        preserve_recent: int = 5,
    ) -> Tuple[List[Any], bool]:
        """
        Compress context if needed by summarizing the entire conversation.

        Args:
            session_id: Chat session ID
            user_id: User ID
            messages: Current message list
            model_name: Model being used
            preserve_recent: Unused - kept for compatibility

        Returns:
            Tuple of (new_messages_list, was_compressed)
        """
        if not cls.should_summarize_context(messages, model_name):
            return messages, False

        logger.info(
            f"Context limit approaching for session {session_id}, compressing entire conversation"
        )

        try:
            # Summarize ALL messages in the conversation
            logger.info(
                f"Summarizing entire conversation with {len(messages)} messages"
            )
            summary_text = cls.create_conversation_summary(messages)

            # Create a system message with the summary that replaces everything
            summary_message = SystemMessage(
                content=f"[CONVERSATION SUMMARY - Previous context was summarized to manage length]\n\n{summary_text}\n\n[END SUMMARY - Current conversation continues below]"
            )

            # Replace ALL messages with just the summary
            new_messages = [summary_message]

            # Store the compression event in database
            cls._store_compression_event(
                session_id, user_id, len(messages), len(new_messages)
            )

            logger.info(
                f"Context compressed: {len(messages)} -> {len(new_messages)} messages"
            )

            return new_messages, True

        except Exception as e:
            logger.error(f"Error compressing context: {e}")
            return messages, False

    @classmethod
    def _store_compression_event(
        cls, session_id: str, user_id: str, old_count: int, new_count: int
    ):
        """Store context compression event in database for tracking."""
        try:
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))

                # Add compression metadata to chat session
                cursor.execute(
                    """
                    UPDATE chat_sessions 
                    SET ui_state = COALESCE(ui_state, '{}'::jsonb) || 
                                  jsonb_build_object(
                                      'context_compressions', 
                                      COALESCE((ui_state->>'context_compressions')::int, 0) + 1,
                                      'last_compression_at', %s,
                                      'last_compression_reduced', %s
                                  )
                    WHERE id = %s AND user_id = %s
                """,
                    (
                        datetime.now().isoformat(),
                        old_count - new_count,
                        session_id,
                        user_id,
                    ),
                )

                conn.commit()
                logger.info(f"Stored compression event for session {session_id}")

        except Exception as e:
            logger.error(f"Error storing compression event: {e}")

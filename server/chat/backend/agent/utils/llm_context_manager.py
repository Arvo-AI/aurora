"""
LLM Context History Manager for Aurora Chat

Handles the storage and retrieval of chat history formatted specifically for LLM context.
This is separate from UI messages to maintain the exact format the agent expects.
"""

import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
import psycopg2
from psycopg2.extras import RealDictCursor
import re

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


class LLMContextManager:
    """Manages LLM context history for chat sessions."""
    
    @staticmethod
    def sanitize_content(content: str) -> str:
        """Sanitize message content to prevent database storage issues."""
        if not isinstance(content, str):
            return str(content)
        
        # Remove null bytes and other problematic characters
        # Replace null bytes with a safe placeholder
        sanitized = content.replace('\x00', '[NULL_BYTE]')
        
        # Remove other control characters that might cause issues
        # Remove control characters except newlines and tabs
        sanitized = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '[CTRL_CHAR]', sanitized)
        
        # Truncate if too long to prevent database issues
        if len(sanitized) > 10000:
            sanitized = sanitized[:10000] + "\n... (truncated due to length)"
        
        return sanitized

    @staticmethod
    def serialize_message(message: Any) -> Dict[str, Any]:
        """Convert a LangChain message object to a dict for storage."""
        
        # Get message type - normalize all AI message variants to 'ai'
        class_name = message.__class__.__name__
        if 'Human' in class_name:
            msg_type = 'human'
        elif 'AI' in class_name:  # Handles AIMessage, AIMessageChunk
            msg_type = 'ai'
        elif 'System' in class_name:
            msg_type = 'system'
        elif 'Tool' in class_name:
            msg_type = 'tool'
        elif hasattr(message, 'type'):
            # Fallback to .type attribute
            msg_type = message.type
        else:
            msg_type = 'unknown'
        
        # Get the content and sanitize it
        content = getattr(message, 'content', '')
        if isinstance(content, str):
            content = LLMContextManager.sanitize_content(content)
        
        # Base message structure
        serialized = {
            "role": msg_type,
            "content": content,
            "timestamp": datetime.now().isoformat()
        }
        
        # Capture the message ID to maintain tool call associations
        if hasattr(message, 'id') and message.id:
            serialized["id"] = message.id
            logger.debug(f"Captured message ID: {message.id}")
        
        # Capture tool calls for AI messages
        if hasattr(message, 'tool_calls') and message.tool_calls:
            logger.info(f"Message has {len(message.tool_calls)} tool calls")
            serialized["tool_calls"] = []
            for tool_call in message.tool_calls:
                if isinstance(tool_call, dict):
                    # Already a dictionary, just use it
                    serialized["tool_calls"].append(tool_call)
                else:
                    # It's an object, extract attributes
                    tool_call_data = {
                        "id": getattr(tool_call, 'id', None),
                        "name": getattr(tool_call, 'name', None),
                        "args": getattr(tool_call, 'args', {}),
                        "type": "tool_call"
                    }
                    serialized["tool_calls"].append(tool_call_data)
            logger.info(f"Serialized tool calls: {serialized['tool_calls']}")
        
        # Capture tool call ID for tool messages
        if hasattr(message, 'tool_call_id'):
            serialized["tool_call_id"] = message.tool_call_id
            
        # Capture additional context for tool messages
        if msg_type == 'tool':
            serialized["tool_result"] = True
            
        # Capture any additional attributes that might be relevant
        for attr in ['name', 'additional_kwargs', 'response_metadata']:
            if hasattr(message, attr):
                value = getattr(message, attr)
                if value:
                    serialized[attr] = value
        
        return serialized
    
    @staticmethod
    def deserialize_message(message_dict: Dict[str, Any]) -> Any:
        """Convert a dict back to a LangChain message object."""
        role = message_dict.get("role", "unknown")
        content = message_dict.get("content", "")
        
        # Preserve the original message ID to maintain tool call associations
        message_id = message_dict.get("id", None)
        
        if role == "human":
            msg = HumanMessage(content=content)
            if message_id:
                msg.id = message_id
            return msg
        elif role == "ai":
            msg = AIMessage(content=content)
            if message_id:
                msg.id = message_id
            # Restore tool calls if present
            if "tool_calls" in message_dict:
                # Note: This is a simplified restoration - in practice, you'd need to
                # recreate proper tool call objects based on your LangChain version
                msg.tool_calls = message_dict["tool_calls"]
            return msg
        elif role == "system":
            msg = SystemMessage(content=content)
            if message_id:
                msg.id = message_id
            return msg
        elif role == "tool":
            tool_call_id = message_dict.get("tool_call_id", "")
            msg = ToolMessage(content=content, tool_call_id=tool_call_id)
            if message_id:
                msg.id = message_id
            # Note: tool_call_id restoration happens in workflow.py
            return msg
        else:
            msg = HumanMessage(content=content)
            if message_id:
                msg.id = message_id
            return msg
    
    
    @staticmethod
    def save_context_history(session_id: str, user_id: str, messages: List[Any], tool_capture=None) -> bool:
        """Save the complete LLM context history - now uses optimized system."""
        # Import here to avoid circular dependency
        from .persistence.context_manager import ContextManager
        return ContextManager.save_context_history(session_id, user_id, messages, tool_capture)
    
    @staticmethod
    def load_context_history(session_id: str, user_id: str) -> List[Any]:
        """Load the complete LLM context history from the database."""
        try:
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor(cursor_factory=RealDictCursor)
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                cursor.execute("""
                    SELECT llm_context_history 
                    FROM chat_sessions 
                    WHERE id = %s AND user_id = %s AND is_active = true
                """, (session_id, user_id))
                
                result = cursor.fetchone()
                
                if not result or not result['llm_context_history']:
                    logger.info(f"No LLM context history found for session {session_id}")
                    return []
                
                serialized_messages = result['llm_context_history']
                if isinstance(serialized_messages, str):
                    serialized_messages = json.loads(serialized_messages)
                
                messages = [LLMContextManager.deserialize_message(msg) for msg in serialized_messages]
                
                logger.info(f"Loaded {len(messages)} messages from LLM context history for session {session_id}")
                return messages
                
        except Exception as e:
            logger.error(f"Error loading LLM context history: {e}")
            return []

    @staticmethod 
    def capture_tool_interaction(session_id: str, user_id: str, tool_name: str, tool_input: Dict[str, Any], tool_output: Any, tool_call_id: str = None) -> bool:
        """Capture a complete tool interaction (call + result) and append to context."""
        try:
            # Load existing context
            existing_messages = LLMContextManager.load_context_history(session_id, user_id)
            
            # Create tool call message
            tool_call_msg = AIMessage(content=f"I'll use the {tool_name} tool.")
            # Add tool call information
            if not hasattr(tool_call_msg, 'tool_calls'):
                tool_call_msg.tool_calls = []
            
            actual_tool_call_id = tool_call_id or f"{tool_name}_{datetime.now().timestamp()}"
            tool_call_data = {
                "id": actual_tool_call_id,
                "name": tool_name,
                "args": tool_input,
                "type": "tool_call"
            }
            tool_call_msg.tool_calls.append(tool_call_data)
            
            # Create tool result message
            tool_result_msg = ToolMessage(
                content=str(tool_output),
                tool_call_id= actual_tool_call_id
            )
            
            # Append to existing context
            updated_messages = existing_messages + [tool_call_msg, tool_result_msg]
            
            # Save updated context
            return LLMContextManager.save_context_history(session_id, user_id, updated_messages)
            
        except Exception as e:
            logger.error(f"Error capturing tool interaction: {e}")
            return False

    @staticmethod
    def migrate_legacy_session(session_id: str, user_id: str, ui_messages: List[Any]) -> bool:
        """Migrate a legacy session by extracting LLM context from UI messages."""
        try:
            if not ui_messages:
                return True  # Nothing to migrate
                
            # Convert UI messages to LLM context format
            llm_messages = []
            for ui_msg in ui_messages:
                if isinstance(ui_msg, dict):
                    sender = ui_msg.get('sender', 'unknown')
                    text = ui_msg.get('text', '')
                    
                    # Skip thinking messages and empty messages
                    if ui_msg.get('isThinking') or not text.strip():
                        continue
                        
                    if sender == 'user':
                        llm_messages.append(HumanMessage(content=text))
                    elif sender == 'bot':
                        llm_messages.append(AIMessage(content=text))
            
            # Save the migrated context
            if llm_messages:
                success = LLMContextManager.save_context_history(session_id, user_id, llm_messages)
                if success:
                    logger.info(f"Migrated legacy session {session_id} with {len(llm_messages)} messages")
                return success
            return True
            
        except Exception as e:
            logger.error(f"Error migrating legacy session {session_id}: {e}")
            return False 
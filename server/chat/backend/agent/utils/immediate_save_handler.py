"""Immediate save handler for user messages - keeps main_chatbot.py clean."""

import json
import time
import logging
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def handle_immediate_save(session_id: str, user_id: str, question: str) -> bool:
    """Persist the user's UI message immediately on receipt.

    We intentionally do NOT write to llm_context_history here. That column is
    fully replaced by the UPDATE in ContextManager._execute_actual_save, so
    passing only the new HumanMessage would wipe the accumulated agent context
    (RCA chat history loss bug). The end-of-stream save in workflow.stream()
    is authoritative for llm_context_history and runs synchronously.

    The UI-formatted message is still appended here so a websocket disconnect
    mid-turn does not lose the user's prompt.
    """
    try:
        ui_messages = [{
            'message_number': 1,
            'text': question,
            'sender': 'user',
            'isCompleted': True,
            'timestamp': time.time()
        }]
        return _save_ui_message(session_id, user_id, ui_messages)

    except Exception as save_error:
        logger.error(f"Error in immediate save: {save_error}")
        return False


def _save_ui_message(session_id: str, user_id: str, ui_messages: List[Dict[str, Any]]) -> bool:
    """Save UI-formatted message to database.
    
    Args:
        session_id: Chat session ID
        user_id: User ID
        ui_messages: UI message data
        
    Returns:
        bool: True if save was successful
    """
    try:
        from utils.db.db_utils import connect_to_db_as_user
        
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        
        cursor.execute("""
            SELECT messages FROM chat_sessions 
            WHERE id = %s AND user_id = %s AND is_active = true
        """, (session_id, user_id))
        
        result = cursor.fetchone()
        if result and result[0]:
            existing_messages = result[0] if isinstance(result[0], list) else []
            # Append new message with proper numbering
            ui_messages[0]['message_number'] = len(existing_messages) + 1
            existing_messages.extend(ui_messages)
            
            # Update with combined messages
            cursor.execute("""
                UPDATE chat_sessions 
                SET messages = %s, updated_at = %s
                WHERE id = %s AND user_id = %s
            """, (json.dumps(existing_messages), datetime.now(), session_id, user_id))
        else:
            # Create new messages array
            cursor.execute("""
                UPDATE chat_sessions 
                SET messages = %s, updated_at = %s
                WHERE id = %s AND user_id = %s
            """, (json.dumps(ui_messages), datetime.now(), session_id, user_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        logger.info(f"✓ Immediately saved UI message for session {session_id}")
        return True
        
    except Exception as ui_save_error:
        logger.warning(f"Failed to immediately save UI message: {ui_save_error}")
        return False

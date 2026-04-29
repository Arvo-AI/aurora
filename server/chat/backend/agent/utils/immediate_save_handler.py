"""Immediate save handler for user messages - keeps main_chatbot.py clean."""

import asyncio
import json
import time
import logging
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


def _dual_write_user_event(
    session_id: str,
    user_id: str,
    text: str,
    message_id: str,
) -> None:
    try:
        from utils.auth.stateless_auth import resolve_org_id
        from chat.backend.agent.utils.persistence.chat_events import (
            record_event,
            upsert_message_projection,
        )

        org_id = resolve_org_id(user_id)
        if not org_id:
            return

        async def _emit():
            await record_event(
                session_id=session_id,
                org_id=org_id,
                type='user_message',
                payload={'text': text},
                message_id=message_id,
            )
            await upsert_message_projection(
                message_id=message_id,
                session_id=session_id,
                org_id=org_id,
                role='user',
                status='complete',
                parts=[{'type': 'text', 'text': text}],
                finalized=True,
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_emit())
        except RuntimeError:
            asyncio.run(_emit())
    except Exception as e:
        logger.warning("[chat_events:dual_write_failed] %s", e)


def handle_immediate_save(session_id: str, user_id: str, question: str) -> bool:
    """Append the user's UI message on receipt so a mid-turn websocket drop doesn't lose it.

    Does NOT touch llm_context_history: ContextManager._execute_actual_save
    fully replaces that column, so writing only the new HumanMessage here
    would wipe the accumulated agent context. The end-of-stream save in
    workflow.stream() is authoritative for llm_context_history.
    """
    try:
        # NOTE: We intentionally do NOT save to llm_context_history here.
        # save_context_history overwrites the entire column, which would
        # replace the full conversation history with just [HumanMessage],
        # destroying all prior context. The workflow handles the full
        # context save after processing.

        # Save UI-formatted message immediately (append-based, safe)
        message_id = str(uuid.uuid4())
        ui_messages = [{
            'message_number': 1,
            'id': message_id,
            'text': question,
            'sender': 'user',
            'isCompleted': True,
            'timestamp': time.time()
        }]

        # Try to load existing UI messages and append
        ui_save_success = _save_ui_message(session_id, user_id, ui_messages)

        if ui_save_success:
            _dual_write_user_event(session_id, user_id, question, message_id)

        return ui_save_success
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
        from utils.auth.stateless_auth import set_rls_context
        
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        if not set_rls_context(cursor, conn, user_id, log_prefix="[ImmediateSave]"):
            conn.close()
            return False
        
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

import logging
import json
import uuid
from flask import Blueprint, request, jsonify, session
from datetime import datetime
from utils.db.db_utils import connect_to_db_as_user
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.web.limiter_ext import limiter


# Configure logging
logging.basicConfig(level=logging.INFO)

chat_bp = Blueprint('chat', __name__)

# Maximum length for chat session titles (in characters)
TITLE_MAX_LENGTH = 50

def generate_chat_title(messages):
    """Generate a chat title from the first few words of the first user message."""
    if not messages or len(messages) == 0:
        return "New Chat"
    
    # Find the first user message
    first_user_message = None
    for message in messages:
        if message.get('sender') == 'user':
            first_user_message = message.get('text', '')
            break
    
    if not first_user_message:
        return "New Chat"
    
    # Take first TITLE_MAX_LENGTH characters and trim to last complete word
    title = first_user_message[:TITLE_MAX_LENGTH]
    if len(first_user_message) > TITLE_MAX_LENGTH:
        last_space = title.rfind(' ')
        if last_space > 0:
            title = title[:last_space]
        title += "..."
    
    return title

@chat_bp.route('/sessions', methods=['GET'])
@limiter.exempt
def get_chat_sessions():
    """Get all chat sessions for a user."""
    
    try:
        # Get authenticated user from X-User-ID header
        user_id = get_user_id_from_request()
        
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401

        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        
        # Set user context for RLS
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Fetch chat sessions ordered by updated_at descending
        # Include both user's own sessions and demo incident sessions
        cursor.execute("""
            SELECT id, title, created_at, updated_at, 
                   CASE WHEN messages IS NULL THEN '[]'::jsonb ELSE messages END as messages,
                   CASE WHEN ui_state IS NULL THEN '{}'::jsonb ELSE ui_state END as ui_state,
                   COALESCE(status, 'active') as status
            FROM chat_sessions 
            WHERE (user_id = %s OR incident_id IN (
                      SELECT id FROM incidents WHERE (alert_metadata->>'is_demo')::boolean = true
                  ))
              AND is_active = true
            ORDER BY updated_at DESC
        """, (user_id,))
        
        sessions = cursor.fetchall()
        
        result = []
        for session_data in sessions:
            session_dict = {
                'id': session_data[0],
                'title': session_data[1],
                'created_at': session_data[2].isoformat() if session_data[2] else None,
                'updated_at': session_data[3].isoformat() if session_data[3] else None,
                'message_count': len(session_data[4]) if session_data[4] else 0,
                'ui_state': session_data[5] if session_data[5] else {},
                'status': session_data[6] if session_data[6] else 'active'
            }
            result.append(session_dict)
        
        return jsonify({'sessions': result}), 200
        
    except Exception as e:
        logging.error(f"Error fetching chat sessions: {e}", exc_info=True)
        return jsonify({'error': 'Failed to fetch chat sessions'}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

@chat_bp.route('/sessions', methods=['POST'])
def create_chat_session():
    """Create a new chat session."""
    try:
        # Get authenticated user from X-User-ID header
        user_id = get_user_id_from_request()
        
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
            
        data = request.get_json()
        title = data.get('title')
        messages = data.get('messages', [])
        ui_state = data.get('ui_state', {})
        
        # Generate title from messages if not provided
        if not title:
            title = generate_chat_title(messages)
        
        # Generate unique session ID
        session_id = str(uuid.uuid4())
        
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        
        # Set user context for RLS
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Insert new chat session
        cursor.execute("""
            INSERT INTO chat_sessions (id, user_id, title, messages, ui_state, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (session_id, user_id, title, json.dumps(messages), json.dumps(ui_state), datetime.now(), datetime.now()))
        
        conn.commit()
        
        # Prepare response data
        response_data = {
            'id': session_id,
            'title': title,
            'messages': messages,
            'ui_state': ui_state,
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'status': 'active'
        }
        
        # Close connection BEFORE returning response to ensure transaction is fully committed
        cursor.close()
        conn.close()
        
        return jsonify(response_data), 201
        
    except Exception as e:
        logging.error(f"Error creating chat session: {e}", exc_info=True)
        return jsonify({'error': 'Failed to create chat session'}), 500
    finally:
        # Cleanup connections if they weren't closed in the success path
        if 'cursor' in locals() and cursor and not cursor.closed:
            cursor.close()
        if 'conn' in locals() and conn and not conn.closed:
            conn.close()

@chat_bp.route('/sessions/<session_id>', methods=['GET'])
@limiter.exempt
def get_chat_session(session_id):
    """Get a specific chat session."""
    try:
        # Get authenticated user from X-User-ID header
        user_id = get_user_id_from_request()
        
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401

        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        
        # Set user context for RLS
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Fetch specific chat session (allow completed, but not cancelled)
        # Allow demo incident chat sessions to be visible to all users
        cursor.execute("""
            SELECT id, title, messages, created_at, updated_at,
                   CASE WHEN ui_state IS NULL THEN '{}'::jsonb ELSE ui_state END as ui_state,
                   COALESCE(status, 'active') as status
            FROM chat_sessions 
            WHERE id = %s 
              AND (user_id = %s OR incident_id IN (
                  SELECT id FROM incidents WHERE (alert_metadata->>'is_demo')::boolean = true
              ))
              AND is_active = true 
              AND status != 'cancelled'
        """, (session_id, user_id))
        
        session_data = cursor.fetchone()
        
        if not session_data:
            return jsonify({'error': 'Chat session not found'}), 404
        
        # Parse messages and extract images from multimodal content
        raw_messages = session_data[2] if session_data[2] else []
        parsed_messages = []
        
        for msg in raw_messages:
            text_content = msg.get('text')
            
            # Check if text is a stringified list (old format from before the fix)
            if isinstance(text_content, str) and text_content.startswith('[{'):
                try:
                    import ast
                    # Parse the string representation back to a Python list
                    text_content = ast.literal_eval(text_content)
                except (ValueError, SyntaxError) as e:
                    logging.warning(f"Failed to parse stringified multimodal content: {e}")
                    # Keep as string if parsing fails
            
            # Handle multimodal content (list with text and image_url parts)
            if isinstance(text_content, list):
                text_parts = []
                images = []
                
                for part in text_content:
                    if isinstance(part, dict):
                        if part.get('type') == 'text':
                            text_parts.append(part.get('text', ''))
                        elif part.get('type') == 'image_url' and 'image_url' in part:
                            # Extract data URL and parse it
                            data_url = part['image_url'].get('url', '')
                            if data_url.startswith('data:'):
                                # Parse data URL: data:image/png;base64,<data>
                                try:
                                    parts = data_url.split(',', 1)
                                    if len(parts) == 2:
                                        header = parts[0]  # data:image/png;base64
                                        data = parts[1]    # base64 data
                                        
                                        # Extract MIME type
                                        mime_type = header.split(';')[0].replace('data:', '')
                                        
                                        images.append({
                                            'displayData': data_url,  # Full data URL for display
                                            'type': mime_type,
                                            'data': data,
                                            'name': f'image_{len(images)}.{mime_type.split("/")[-1]}'
                                        })
                                except Exception as e:
                                    logging.error(f"Error parsing image data URL: {e}")
                
                # Update message with parsed content
                msg['text'] = ' '.join(text_parts)
                if images:
                    msg['images'] = images
            
            parsed_messages.append(msg)
        
        result = {
            'id': session_data[0],
            'title': session_data[1],
            'messages': parsed_messages,
            'created_at': session_data[3].isoformat() if session_data[3] else None,
            'updated_at': session_data[4].isoformat() if session_data[4] else None,
            'ui_state': session_data[5] if session_data[5] else {},
            'status': session_data[6] if session_data[6] else 'active'
        }
        
        return jsonify(result), 200
        
    except Exception as e:
        logging.error(f"Error fetching chat session: {e}", exc_info=True)
        return jsonify({'error': 'Failed to fetch chat session'}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

@chat_bp.route('/sessions/<session_id>', methods=['PUT'])
def update_chat_session(session_id):
    """Update a chat session."""
    try:
        # Get authenticated user from X-User-ID header
        user_id = get_user_id_from_request()
        
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
            
        data = request.get_json()
        title = data.get('title')
        messages = data.get('messages')
        ui_state = data.get('ui_state')
        
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        
        # Set user context for RLS
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Check if session exists and is not cancelled
        cursor.execute("""
            SELECT id, status FROM chat_sessions 
            WHERE id = %s AND user_id = %s AND is_active = true
        """, (session_id, user_id))
        
        session_row = cursor.fetchone()
        if not session_row:
            return jsonify({'error': 'Chat session not found'}), 404
        
        # Prevent updates to cancelled or completed sessions
        session_status = session_row[1] if len(session_row) > 1 else 'active'
        if session_status in ('cancelled', 'completed'):
            return jsonify({'error': 'Cannot update a cancelled or completed session'}), 403
        
        # Prepare update fields
        update_fields = []
        update_values = []
        
        if title is not None:
            update_fields.append("title = %s")
            update_values.append(title)
        
        if messages is not None:
            update_fields.append("messages = %s")
            update_values.append(json.dumps(messages))
            
            # Only auto-generate title if updating messages and no title provided AND the session has no existing title or has default title
            if title is None:
                # First, check if the session has an existing custom title
                cursor.execute("""
                    SELECT title FROM chat_sessions 
                    WHERE id = %s AND user_id = %s AND is_active = true
                """, (session_id, user_id))
                
                existing_session = cursor.fetchone()
                existing_title = existing_session[0] if existing_session else None
                
                # Only auto-generate if there's no existing title or if the existing title is "New Chat" (default)
                if not existing_title or existing_title == "New Chat":
                    new_title = generate_chat_title(messages)
                    update_fields.append("title = %s")
                    update_values.append(new_title)
        
        if ui_state is not None:
            update_fields.append("ui_state = %s")
            update_values.append(json.dumps(ui_state))
        
        update_fields.append("updated_at = %s")
        update_values.append(datetime.now())
        
        # Add session_id and user_id for WHERE clause
        update_values.extend([session_id, user_id])
        
        # Update chat session
        cursor.execute(f"""
            UPDATE chat_sessions 
            SET {', '.join(update_fields)}
            WHERE id = %s AND user_id = %s
        """, update_values)
        
        conn.commit()
        
        # Fetch updated session
        cursor.execute("""
            SELECT id, title, messages, created_at, updated_at,
                   CASE WHEN ui_state IS NULL THEN '{}'::jsonb ELSE ui_state END as ui_state,
                   COALESCE(status, 'active') as status
            FROM chat_sessions 
            WHERE id = %s AND user_id = %s AND is_active = true
        """, (session_id, user_id))
        
        session_data = cursor.fetchone()
        
        result = {
            'id': session_data[0],
            'title': session_data[1],
            'messages': session_data[2] if session_data[2] else [],
            'created_at': session_data[3].isoformat() if session_data[3] else None,
            'updated_at': session_data[4].isoformat() if session_data[4] else None,
            'ui_state': session_data[5] if session_data[5] else {},
            'status': session_data[6] if session_data[6] else 'active'
        }
        
        return jsonify(result), 200
        
    except Exception as e:
        logging.error(f"Error updating chat session: {e}", exc_info=True)
        return jsonify({'error': 'Failed to update chat session'}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

@chat_bp.route('/sessions/<session_id>', methods=['DELETE'])
def delete_chat_session(session_id):
    """Delete a chat session (soft delete)."""
    try:
        # Get authenticated user from X-User-ID header
        user_id = get_user_id_from_request()
        
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
        logging.info(f"Deleting chat session {session_id} for user {user_id}")
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        
        # Set user context for RLS
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Check if session exists
        cursor.execute("""
            SELECT id FROM chat_sessions 
            WHERE id = %s AND user_id = %s AND is_active = true
        """, (session_id, user_id))
        
        if not cursor.fetchone():
            return jsonify({'error': 'Chat session not found'}), 404
        
        # Soft delete the session
        cursor.execute("""
            UPDATE chat_sessions 
            SET is_active = false, updated_at = %s
            WHERE id = %s AND user_id = %s
        """, (datetime.now(), session_id, user_id))
        
        conn.commit()

        # Delete storage files associated with this session
        # TODO: This only works for terraform files, not other files in the session
        try:
            from utils.storage.storage import get_storage_manager
            storage = get_storage_manager(user_id=user_id)
            prefix = f"users/{user_id}/{session_id}/terraform_dir/"
            deleted_count = storage.delete_files_with_prefix(prefix)
            logging.info(f"Deleted {deleted_count} storage files for session {session_id} and user {user_id}")
        except Exception as storage_exc:
            logging.error(f"Failed to delete storage files for session {session_id}: {storage_exc}")

        return jsonify({'message': 'Chat session deleted successfully'}), 200
        
    except Exception as e:
        logging.error(f"Error deleting chat session: {e}", exc_info=True)
        return jsonify({'error': 'Failed to delete chat session'}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close() 

@chat_bp.route('/sessions/bulk-delete', methods=['DELETE'])
def delete_all_chat_sessions():
    """Delete all chat sessions for a user (soft delete)."""
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401

        current_session_id = request.args.get('current_session_id')
        logging.info(f"Bulk delete request - current_session_id: {current_session_id}")

        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        if current_session_id:
            logging.info(f"Preserving session {current_session_id}, deleting all others")
            cursor.execute("""
                UPDATE chat_sessions 
                SET is_active = false, updated_at = %s
                WHERE user_id = %s AND is_active = true AND id != %s
            """, (datetime.now(), user_id, current_session_id))
        else:
            logging.info("No current session to preserve, deleting all sessions")
            cursor.execute("""
                UPDATE chat_sessions 
                SET is_active = false, updated_at = %s
                WHERE user_id = %s AND is_active = true
            """, (datetime.now(), user_id))
        
        deleted_count = cursor.rowcount
        logging.info(f"Deleted {deleted_count} chat sessions")
        conn.commit()
        
        if current_session_id:
            return jsonify({'message': f'Successfully deleted {deleted_count} chat sessions (preserved current session)'}), 200
        else:
            return jsonify({'message': f'Successfully deleted {deleted_count} chat sessions'}), 200
        
    except Exception as e:
        logging.error(f"Error deleting chat sessions: {e}", exc_info=True)
        return jsonify({'error': 'Failed to delete chat sessions'}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close() 




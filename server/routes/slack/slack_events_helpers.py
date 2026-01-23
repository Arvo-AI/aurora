"""
Helper functions for Slack Events API handling.
"""

import logging
import os
import hmac
import hashlib
import time
from typing import Optional
from utils.db.connection_pool import db_pool
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Maximum length for chat session titles (in characters)
TITLE_MAX_LENGTH = 50

# Get Slack signing secret from environment
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

if not SLACK_SIGNING_SECRET:
    raise ValueError("SLACK_SIGNING_SECRET not configured")

# Slack text length limits
SLACK_MAX_SECTION_TEXT = 3000  # Slack Block Kit section text limit
SLACK_MAX_MESSAGE_LENGTH = 3900  # Safe limit for Slack message (actual limit is 4000)
SLACK_SECTION_TEXT_BUFFER = 2900  # Safe buffer for section text with formatting
COMMAND_DISPLAY_TRUNCATE_LENGTH = 150  # Length for truncating commands in thread replies
COMMAND_FULL_DISPLAY_LENGTH = 500  # Length for truncating full command display in suggestions
SLACK_MAX_BLOCKS = 50  # Slack Block Kit blocks limit per message


def verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Verify that the request came from Slack by validating the signature.
    https://api.slack.com/authentication/verifying-requests-from-slack
    """
    
    # Check timestamp is recent (within 5 minutes) to avoid replay attacks (attacker could put their hands on old valid request)
    try:
        request_time = int(timestamp)
        if abs(time.time() - request_time) > 60 * 5:
            logger.warning(f"Slack request timestamp too old: {timestamp}")
            return False
    except (ValueError, TypeError):
        logger.error(f"Invalid Slack timestamp: {timestamp}")
        return False
    
    # Verify signature
    sig_basestring = f"v0:{timestamp}:{request_body.decode('utf-8')}"
    expected_signature = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_signature, signature)


def get_user_id_from_slack_team(team_id: str) -> Optional[str]:
    """
    Find Aurora user_id from Slack team_id (workspace ID).
    Returns the first active user connected to this Slack workspace.
    Note: team_id is stored in the subscription_id column for Slack.

    This is to send a message into a channel where the requesting user is not authenticated. (We need credentials to send a message)
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Query user_tokens for Slack users with matching team_id (stored in subscription_id)
                cursor.execute(
                    """
                    SELECT user_id 
                    FROM user_tokens 
                    WHERE provider = 'slack' 
                    AND subscription_id = %s
                    AND is_active = TRUE
                    LIMIT 1
                    """,
                    (team_id,)
                )
                result = cursor.fetchone()
                
                if result:
                    return result[0]
                
                return None
    except Exception as e:
        logger.error(f"Error looking up user from Slack team_id {team_id}: {e}", exc_info=True)
        return None


def get_user_id_from_slack_user(slack_user_id: str, team_id: str) -> Optional[str]:
    """
    Find Aurora user_id from Slack user_id.
    Checks if the Slack user who @mentioned Aurora is authenticated in Aurora.
    Note: team_id is stored in the subscription_id column for Slack.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Query user_tokens for Slack users with matching team_id (stored in subscription_id)
                cursor.execute(
                    """
                    SELECT user_id, secret_ref 
                    FROM user_tokens 
                    WHERE provider = 'slack' 
                    AND subscription_id = %s
                    AND is_active = TRUE
                    """,
                    (team_id,)
                )
                results = cursor.fetchall()
                
                if not results:
                    return None
                
                from utils.secrets.secret_ref_utils import get_user_token_data
                
                for user_id, secret_ref in results:
                    try:
                        token_data = get_user_token_data(user_id, "slack")
                        if not token_data:
                            continue
                        
                        # Check if Slack user_id matches
                        stored_slack_user_id = token_data.get('user_id')  # Slack user ID of the person who connected
                        
                        # Match if same Slack user (team_id already matched in query)
                        if stored_slack_user_id == slack_user_id:
                            return user_id
                    except Exception as e:
                        logger.debug(f"Error checking user {user_id}: {e}")
                        continue
                
                return None
    except Exception as e:
        logger.error(f"Error looking up user from Slack user_id {slack_user_id}: {e}", exc_info=True)
        return None


def _get_user_display_name(client, user_id: str) -> str:
    """
    Get display name for a Slack user ID.
    Returns display name or falls back to user ID if lookup fails.
    """
    if not user_id or user_id == 'unknown':
        return 'Unknown User'
    
    try:
        result = client._make_request("GET", "users.info", {"user": user_id})
        user_info = result.get('user', {})
        
        # Prefer display_name, fall back to real_name, then user_id. 
        # Could fail if missing permissions or user is from out of organization. (Hence the user_id fallback)
        display_name = (
            user_info.get('profile', {}).get('display_name') or
            user_info.get('profile', {}).get('real_name') or
            user_info.get('name') or
            user_id
        )
        return display_name.strip() if display_name else user_id
    except Exception as e:
        logger.debug(f"Could not fetch user info for {user_id}: {e}")
        return user_id


def _resolve_user_display_name(client, user_id: str, is_bot: bool, user_name_cache: dict) -> str:
    """
    Resolve display name for a user with caching.
    Returns "Aurora" for bots, otherwise looks up and caches user display name.
    """
    if is_bot:
        return "Aurora"
    
    if user_id not in user_name_cache:
        user_name_cache[user_id] = _get_user_display_name(client, user_id)
    
    return user_name_cache[user_id]


def _format_slack_timestamp(ts: str) -> str:
    """Convert Slack timestamp to readable format."""
    try:
        ts_float = float(ts)
        dt = datetime.fromtimestamp(ts_float)
        return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    except (ValueError, TypeError, OSError):
        return ""


def get_thread_messages(client, channel: str, thread_ts: str, limit: int = 10, max_message_length: int = 5000, max_total_length: int = 50000):
    """
    Fetch messages from a Slack thread for context.
    Returns list of messages in chronological order with resolved user names.
    """
    try:
        result = client._make_request(
            "GET",
            "conversations.replies",
            {"channel": channel, "ts": thread_ts, "limit": limit}
        )
        messages = result.get('messages', [])
        
        # Cache for user display names to avoid repeated API calls
        user_name_cache = {}
        
        # Format messages for context
        # Process in reverse to prioritize recent messages when truncating
        formatted = []
        total_length = 0
        
        for msg in reversed(messages):
            text = msg.get('text', '')
            
            # Skip empty messages
            if not text.strip():
                continue
            
            # Truncate individual message if too long
            if len(text) > max_message_length:
                text = text[:max_message_length] + f"\n... [message truncated from {len(text)} to {max_message_length} characters]"
            
            # Check if adding this message would exceed total limit
            if total_length + len(text) > max_total_length:
                break #Has the most recent messages but ignores remaining messages
            
            # Check if this is a bot message
            is_bot = msg.get('bot_id') is not None
            user_id = msg.get('user', 'unknown')
            timestamp = msg.get('ts', '')
            
            # Resolve user display name (with caching)
            display_name = _resolve_user_display_name(client, user_id, is_bot, user_name_cache)
            
            formatted.append({
                'user': user_id if not is_bot else 'bot',
                'display_name': display_name,
                'text': text,
                'timestamp': timestamp,
                'is_bot': is_bot
            })
            
            total_length += len(text)
        
        # Reverse back to chronological order for display
        return list(reversed(formatted))
    except Exception as e:
        logger.error(f"Error fetching thread messages: {e}", exc_info=True)
        return []


def get_channel_context_with_threads(client, channel: str, limit: int = 5, max_total_length: int = 50000):
    """
    Fetch recent channel messages and thread summaries for context.
    Returns formatted string with:
    - Last N messages from main channel
    - For each message with a thread: first 3 and last 3 messages from that thread
    """
    try:
        # Fetch recent channel messages
        result = client._make_request(
            "GET",
            "conversations.history",
            {"channel": channel, "limit": limit}
        )
        channel_messages = result.get('messages', [])
        
        if not channel_messages:
            return ""
        
        context_parts = []
        user_name_cache = {}
        total_length = 0
        
        # Process messages from newest to oldest (prioritize recent when truncating)
        for msg in channel_messages:
            text = msg.get('text', '').strip()
            user_id = msg.get('user')
            is_bot = msg.get('bot_id') is not None
            ts = msg.get('ts', '')
            reply_count = msg.get('reply_count', 0)
            thread_ts = msg.get('thread_ts') or ts
            
            # Skip empty messages
            if not text:
                continue
            
            # Resolve user name and format timestamp
            display_name = _resolve_user_display_name(client, user_id, is_bot, user_name_cache)
            readable_time = _format_slack_timestamp(ts)
            timestamp_prefix = f"[{readable_time}] " if readable_time else ""
            
            # Add main message with timestamp
            main_msg = f"• {timestamp_prefix}{display_name}: {text}"
            
            # Check if adding this would exceed total limit
            if total_length + len(main_msg) > max_total_length:
                break #Has the most recent messages but ignores remaining messages
            
            context_parts.append(main_msg)
            total_length += len(main_msg)
            
            # If message has replies, get thread summary
            if reply_count > 0:
                try:
                    thread_result = client._make_request(
                        "GET",
                        "conversations.replies",
                        {"channel": channel, "ts": thread_ts, "limit": 100}
                    )
                    thread_messages = thread_result.get('messages', [])
                    
                    # Exclude parent message from replies
                    thread_replies = [m for m in thread_messages[1:] if m.get('text', '').strip()]
                    
                    if thread_replies:
                        if len(thread_replies) > 6:
                            # Show first 3 and last 3
                            first_3 = thread_replies[:3]
                            last_3 = thread_replies[-3:]
                            
                            thread_header = f"  └─ Thread ({len(thread_replies)} replies):"
                            if total_length + len(thread_header) > max_total_length:
                                break #Has the most recent messages but ignores remaining messages
                            
                            context_parts.append(thread_header)
                            total_length += len(thread_header)
                            
                            for reply in first_3:
                                reply_user = reply.get('user')
                                reply_bot = reply.get('bot_id') is not None
                                reply_name = _resolve_user_display_name(client, reply_user, reply_bot, user_name_cache)
                                reply_ts = reply.get('ts', '')
                                reply_text = reply.get('text', '').strip()
                                reply_time = _format_slack_timestamp(reply_ts)
                                timestamp_prefix = f"[{reply_time}] " if reply_time else ""

                                # Truncate long messages
                                if len(reply_text) > COMMAND_DISPLAY_TRUNCATE_LENGTH:
                                    reply_text = reply_text[:COMMAND_DISPLAY_TRUNCATE_LENGTH] + "..."
                                
                                reply_line = f"    • {timestamp_prefix}{reply_name}: {reply_text}"
                                if total_length + len(reply_line) > max_total_length:
                                    logger.warning(f"Reached total context limit. Stopping at thread reply.")
                                    break
                                
                                context_parts.append(reply_line)
                                total_length += len(reply_line)
                            
                            separator = f"    ... ({len(thread_replies) - 6} more messages) ..."
                            if total_length + len(separator) <= max_total_length:
                                context_parts.append(separator)
                                total_length += len(separator)
                            
                            for reply in last_3:
                                reply_user = reply.get('user')
                                reply_bot = reply.get('bot_id') is not None
                                reply_name = _resolve_user_display_name(client, reply_user, reply_bot, user_name_cache)
                                reply_ts = reply.get('ts', '')
                                reply_text = reply.get('text', '').strip()
                                reply_time = _format_slack_timestamp(reply_ts)

                                if len(reply_text) > COMMAND_DISPLAY_TRUNCATE_LENGTH:
                                    reply_text = reply_text[:COMMAND_DISPLAY_TRUNCATE_LENGTH] + "..."
                                
                                if reply_time:
                                    reply_line = f"    • [{reply_time}] {reply_name}: {reply_text}"
                                else:
                                    reply_line = f"    • {reply_name}: {reply_text}"
                                
                                if total_length + len(reply_line) > max_total_length:
                                    logger.warning(f"Reached total context limit. Stopping at thread reply.")
                                    break
                                
                                context_parts.append(reply_line)
                                total_length += len(reply_line)
                        else:
                            # Fewer than 6 replies, show all
                            thread_header = f"  └─ Thread ({len(thread_replies)} replies):"
                            if total_length + len(thread_header) > max_total_length:
                                logger.warning(f"Reached total context limit. Truncating thread context.")
                                break
                            
                            context_parts.append(thread_header)
                            total_length += len(thread_header)
                            
                            for reply in thread_replies:
                                reply_user = reply.get('user')
                                reply_bot = reply.get('bot_id') is not None
                                reply_name = _resolve_user_display_name(client, reply_user, reply_bot, user_name_cache)
                                reply_ts = reply.get('ts', '')
                                reply_text = reply.get('text', '').strip()
                                reply_time = _format_slack_timestamp(reply_ts)

                                if len(reply_text) > COMMAND_DISPLAY_TRUNCATE_LENGTH:
                                    reply_text = reply_text[:COMMAND_DISPLAY_TRUNCATE_LENGTH] + "..."
                                
                                if reply_time:
                                    reply_line = f"    • [{reply_time}] {reply_name}: {reply_text}"
                                else:
                                    reply_line = f"    • {reply_name}: {reply_text}"
                                
                                if total_length + len(reply_line) > max_total_length:
                                    logger.warning(f"Reached total context limit. Stopping at thread reply.")
                                    break
                                
                                context_parts.append(reply_line)
                                total_length += len(reply_line)
                
                except Exception as e:
                    logger.debug(f"Could not fetch thread for message {ts}: {e}")
        
        # Reverse to chronological order (oldest to newest) for display
        return "\n".join(reversed(context_parts)) if context_parts else ""
        
    except Exception as e:
        logger.error(f"Error fetching channel context: {e}", exc_info=True)
        return ""


def format_response_for_slack(text: str, max_length: int = SLACK_MAX_MESSAGE_LENGTH) -> str:
    """
    Format Aurora's response for Slack by converting markdown and handling length limits.

    We already have instructons in prompt, but this is a fallback in case the prompt is not followed.
    """
    if not text:
        return ""
    
    # Ensure proper line breaks (replace literal \n with actual newlines)
    formatted = text.replace('\\n', '\n')
    
    # Remove null bytes and other problematic characters
    formatted = formatted.replace('\x00', '')
    
    # Remove in-text citations (e.g., [1], [2, 3], [4, 5, 6]) since they can't be rendered on Slack
    # Match patterns like [1] or [1, 2, 3] but preserve markdown links
    formatted = re.sub(r'\[(\d+(?:,\s*\d+)*)\]', '', formatted)
    
    # Clean up spaces before punctuation (left from citation removal)
    formatted = re.sub(r'\s+([.,;:!?])', r'\1', formatted)
    
    # Clean up any double spaces left from citation removal
    formatted = re.sub(r'  +', ' ', formatted)
    
    # Convert markdown formatting to Slack format
    formatted = re.sub(r'\*\*([^\*]+)\*\*', r'*\1*', formatted)
    formatted = re.sub(r'(?<!\*)\*(?!\*)([^\*]+)\*(?!\*)', r'_\1_', formatted) #italic
    formatted = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<\2|\1>', formatted) #links
    formatted = re.sub(r'<(?!http|@|#)([^>]+)>', '', formatted) #remove html tags
    
    # Handle length limits - Slack has 4000 char limit per message
    if len(formatted) > max_length:
        formatted = formatted[:max_length] + "\n\n...(message truncated due to length - see full response in chat history)"
    
    return formatted


def extract_summary_section(text: str) -> str:
    """
    Extract the summary section from an investigation response.
    Returns everything before 'Suggested Next Steps', 'Next Steps', 'Recommendations', etc.
    Handles both the "Current Summary" format and plain responses.
    """
    if not text:
        return ""
    
    # Section markers that indicate end of summary (most specific first)
    end_markers = [
        'Suggested Next Steps',
        'Next Steps',
        'Recommendations',
        'Action Items',
        'Proposed Actions',
        'Remediation Steps', #Added the other ones in case the prompt is not followed.
    ]
    
    # Find the earliest end marker
    earliest_pos = len(text)
    for marker in end_markers:
        for prefix in ['', '\n', ' ', '## ', '### ', '#### ', '\n## ', '\n### ', '\n#### ', '* ', '** ', '\n* ', '\n** ']:
            pattern = prefix + marker
            pos = text.find(pattern)
            if pos != -1 and pos < earliest_pos: # Found an earlier end marker
                earliest_pos = pos
    
    # Extract summary content
    if earliest_pos < len(text):
        summary = text[:earliest_pos].strip()
    else:
        # No markers found - take first 3 paragraphs as fallback
        paragraphs = text.split('\n\n')
        summary = '\n\n'.join(paragraphs[:3]).strip()
    
    return summary


def get_incident_by_slack_message(user_id: str, slack_message_ts: str):
    """
    Find an incident by its Slack notification message timestamp.
    Returns (incident_id, session_id) or (None, None) if not found.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, aurora_chat_session_id
                    FROM incidents
                    WHERE user_id = %s AND slack_message_ts = %s
                    LIMIT 1
                    """,
                    (user_id, slack_message_ts)
                )
                result = cursor.fetchone()
                
                if result:
                    incident_id = str(result[0])
                    session_id = str(result[1]) if result[1] else None
                    logger.info(f"Found incident {incident_id} (session: {session_id}) from slack_message_ts {slack_message_ts}")
                    return incident_id, session_id
                
                return None, None
                
    except Exception as e:
        logger.error(f"Error looking up incident by slack_message_ts: {e}", exc_info=True)
        return None, None


def get_session_from_thread(user_id: str, channel_id: str, thread_ts: str):
    """
    Find the session_id associated with a Slack thread.
    First checks if thread_ts matches an incident notification message.
    Then looks up chat sessions by thread_ts in trigger_metadata.
    Returns (session_id, incident_id) or (None, None) if not found.
    """
    try:
        # First, check if this thread is from an incident notification
        incident_id, session_id = get_incident_by_slack_message(user_id, thread_ts)
        if incident_id:
            return session_id, incident_id
        
        # Otherwise, look up by thread_ts in chat session metadata
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Look up chat session by thread_ts in trigger_metadata
                cursor.execute(
                    """
                    SELECT id, ui_state
                    FROM chat_sessions
                    WHERE user_id = %s
                    AND (ui_state->'triggerMetadata'->>'source') = 'slack'
                    AND (ui_state->'triggerMetadata'->>'channel') = %s
                    AND (ui_state->'triggerMetadata'->>'thread_ts') = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id, channel_id, str(thread_ts))
                )
                result = cursor.fetchone()
                
                if result:
                    session_id = str(result[0])
                    
                    # Try to find associated incident
                    cursor.execute(
                        """
                        SELECT id
                        FROM incidents
                        WHERE user_id = %s AND aurora_chat_session_id = %s
                        LIMIT 1
                        """,
                        (user_id, session_id)
                    )
                    incident_result = cursor.fetchone()
                    incident_id = str(incident_result[0]) if incident_result else None
                    
                    return session_id, incident_id
                
                return None, None
                
    except Exception as e:
        logger.error(f"Error looking up session from thread: {e}", exc_info=True)
        return None, None


def send_message_to_aurora(user_id: str, message_text: str, channel: str, thread_ts: str = None, 
                           incident_id: str = None, session_id: str = None, context_messages: list = None,
                           channel_context: str = None, thinking_message_ts: str = None):
    """
    Route a Slack message to Aurora's chat system.
    Uses background chat task to process the message.
    Creates a chat session in the database so it appears in chat history.
    """
    # Import here to avoid circular dependency with chat.background.task
    from chat.background.task import run_background_chat, create_background_chat_session
    
    try:
        # Create session if not exists
        if not session_id:
            # Generate a title from the message (first TITLE_MAX_LENGTH chars)
            title = 'Slack: ' + (message_text[:TITLE_MAX_LENGTH] + "..." if len(message_text) > TITLE_MAX_LENGTH else message_text)
            
            # Trigger metadata for tracking
            trigger_metadata = {
                "source": "slack",
                "channel": channel,
                "thread_ts": thread_ts,
            }
            
            # Create the session in the database so it appears in chat history
            session_id = create_background_chat_session(
                user_id=user_id,
                title=title,
                trigger_metadata=trigger_metadata
            )
        
        # Build context from thread messages or channel messages
        context_str = ""
        
        if context_messages:
            # Thread context - use all messages (already limited to 10 by get_thread_messages)
            context_str = "\n\n--- Recent conversation context from Slack thread ---\n"
            
            for i, msg in enumerate(context_messages, 1):
                display_name = msg.get('display_name', msg.get('user', 'Unknown'))
                text = msg.get('text', '').strip()
                timestamp = msg.get('timestamp', '')
                readable_time = _format_slack_timestamp(timestamp)
                timestamp_prefix = f"[{readable_time}] " if readable_time else ""
                context_str += f"{i}. {timestamp_prefix}{display_name}: {text}\n"
            
            context_str += "--- End of thread context ---\n"
        
        elif channel_context:
            # Channel context - includes recent messages and thread summaries
            context_str = f"\n\n--- Recent channel context (last 5 messages with thread summaries) ---\n{channel_context}\n--- End of channel context ---\n"
        
        # Prepare the message with context
        full_message = f"{message_text}{context_str}"
        
        # Trigger metadata for tracking and response
        trigger_metadata = {
            "source": "slack",
            "channel": channel,
            "thread_ts": thread_ts,
            "thinking_message_ts": thinking_message_ts,  # Store the thinking message timestamp
        }
        
        # Launch background chat task
        # For Slack @mentions, link to incident but don't send "investigation started" notifications
        run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=full_message,
            trigger_metadata=trigger_metadata,
            provider_preference=None,  # Use default providers
            incident_id=incident_id,
            send_notifications=False  # Don't send investigation started notifications for Slack @mentions
        )
        
        return True
        
    except Exception as e:
        logger.error(f"Error sending Slack message to Aurora: {e}", exc_info=True)
        return False


def get_incident_suggestions(incident_id: str):
    """
    Get runnable suggestions for an incident from the database.
    Returns list of dicts with id, title, description, type, risk, command.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, title, description, type, risk, command
                    FROM incident_suggestions
                    WHERE incident_id = %s
                    ORDER BY 
                        CASE type
                            WHEN 'diagnostic' THEN 1
                            WHEN 'mitigation' THEN 2
                            WHEN 'communication' THEN 3
                            ELSE 4
                        END,
                        created_at ASC
                    """,
                    (incident_id,)
                )
                rows = cursor.fetchall()
                
                suggestions = []
                for row in rows:
                    suggestions.append({
                        'id': row[0],
                        'title': row[1],
                        'description': row[2],
                        'type': row[3],
                        'risk': row[4],
                        'command': row[5],
                    })
                
                return suggestions
    except Exception as e:
        logger.error(f"Error fetching incident suggestions for {incident_id}: {e}", exc_info=True)
        return []


def validate_slack_blocks(blocks: list) -> bool:
    """
    Validate Slack Block Kit blocks before sending.
    Returns True if valid, False otherwise.
    """
    import json
    
    try:
        if not isinstance(blocks, list):
            logger.error(f"[SlackBlocks] Blocks is not a list: {type(blocks)}")
            return False
        
        # Test JSON serialization (Slack requires valid JSON)
        try:
            json.dumps(blocks)
        except (TypeError, ValueError) as e:
            logger.error(f"[SlackBlocks] Blocks not JSON serializable: {e}")
            return False
        
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                logger.error(f"[SlackBlocks] Block {i} is not a dict: {type(block)}")
                return False
            
            if 'type' not in block:
                logger.error(f"[SlackBlocks] Block {i} missing 'type' field")
                return False
            
            block_type = block.get('type')
            
            # Validate text blocks (section, header)
            if block_type in ['section', 'header']:
                if 'text' in block:  # text is optional for sections with only fields/accessory
                    text_obj = block['text']
                    if not isinstance(text_obj, dict):
                        logger.error(f"[SlackBlocks] Block {i} text is not a dict")
                        return False
                    
                    if 'text' not in text_obj:
                        logger.error(f"[SlackBlocks] Block {i} text object missing 'text' field")
                        return False
                    
                    text_content = text_obj['text']
                    if not isinstance(text_content, str):
                        logger.error(f"[SlackBlocks] Block {i} has invalid text content type: {type(text_content)}")
                        return False
                    
                    # Check length limits (Slack has 3000 char limit for section text)
                    if len(text_content) > SLACK_MAX_SECTION_TEXT:
                        logger.error(f"[SlackBlocks] Block {i} text exceeds {SLACK_MAX_SECTION_TEXT} chars: {len(text_content)}")
                        return False
            
            # Validate actions blocks
            elif block_type == 'actions':
                if 'elements' not in block:
                    logger.error(f"[SlackBlocks] Actions block {i} missing 'elements' field")
                    return False
                
                elements = block['elements']
                if not isinstance(elements, list) or len(elements) == 0:
                    logger.error(f"[SlackBlocks] Actions block {i} has invalid elements")
                    return False
        
        logger.info(f"[SlackBlocks] Validated {len(blocks)} blocks successfully")
        return True
    except Exception as e:
        logger.error(f"[SlackBlocks] Validation error: {e}", exc_info=True)
        return False


def build_suggestions_blocks(incident_id: str, suggestions: list, max_suggestions: int = 5) -> list:
    """
    Build Slack Block Kit blocks for runnable suggestions.
    
    Args:
        incident_id: The incident UUID
        suggestions: List of suggestion dicts from get_incident_suggestions()
        max_suggestions: Maximum number of suggestions to show (default 5)
    
    Returns:
        List of Slack Block Kit blocks
    """
    if not suggestions:
        return []
    
    blocks = []
    
    # Header
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": "Suggested Next Steps"
        }
    })
    
    # Show up to max_suggestions
    for i, suggestion in enumerate(suggestions[:max_suggestions]):
        if not suggestion.get('command'):
            continue  # Skip suggestions without commands
        
        # Validate required fields
        title = suggestion.get('title', 'Action')
        if not title:
            title = 'Action'
        
        command = suggestion.get('command', '')
        
        # Truncate command for display (Slack has limits)
        # Show more context - users can click "More details" for full command
        command_display = command[:COMMAND_FULL_DISPLAY_LENGTH] + '... (click More details for full command)' if len(command) > COMMAND_FULL_DISPLAY_LENGTH else command
        
        # Build compact text with just title and command (no description)
        text = f"*{title}*\n`{command_display}`"
        if len(text) > SLACK_SECTION_TEXT_BUFFER:  # Leave buffer for Slack
            text = text[:SLACK_SECTION_TEXT_BUFFER] + "..."
        
        # Build the run button (always green/primary)
        run_button = {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Run"
            },
            "value": f"{incident_id}:{suggestion['id']}",
            "action_id": f"run_suggestion_{suggestion['id']}",
            "style": "primary"  # Always green
        }
        
        # Build actions with Run button and More details button
        action_elements = [run_button]
        
        # Add "More details" button
        details_button = {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "More details"
            },
            "value": f"{incident_id}:{suggestion['id']}:details",
            "action_id": f"suggestion_details_{suggestion['id']}"
        }
        action_elements.append(details_button)
        
        # Build the block with section and actions
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text
            }
        })
        
        blocks.append({
            "type": "actions",
            "elements": action_elements
        })
    
    # Divider
    blocks.append({"type": "divider"})
    
    return blocks


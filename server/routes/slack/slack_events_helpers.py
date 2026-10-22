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
from utils.auth.stateless_auth import set_rls_context
from utils.log_sanitizer import sanitize
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Slack notifies only on this form. A typed @name is plain text.
_SLACK_USER_ID = re.compile(r"[UW][A-Z0-9]{2,}")
# <@U123|Ada> is the same mention as <@U123>. Label stops at "<" so it can't swallow the next one.
_SLACK_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^<>]*)?>")
# Prefix format_asked_by writes. The title drops it so the question stays visible.
_ASKED_BY_PREFIX = re.compile(
    r"^Asked by (?:<@[UW][A-Z0-9]+>(?: \([^)]*\))?|[^:]{1,80}): "
)

# Maximum length for chat session titles (in characters)
TITLE_MAX_LENGTH = 50

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

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
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not configured, rejecting Slack request")
        return False

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


def _get_all_org_ids() -> list:
    """Get all distinct org_ids from the users table (not RLS-protected)."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DISTINCT org_id FROM users WHERE org_id IS NOT NULL")
                return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.exception("Failed to fetch org_ids for Slack webhook bootstrap")
        return []


def _lookup_slack_token(team_id: str, include_secret_ref: bool = False) -> list:
    """
    Query user_tokens for Slack entries matching team_id, iterating orgs to
    bypass FORCE ROW LEVEL SECURITY (which blocks even superuser without context).
    """
    org_ids = _get_all_org_ids()
    if not org_ids:
        return []

    errors = 0
    for org_id in org_ids:
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET myapp.current_org_id = %s", (org_id,))
                    if include_secret_ref:
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
                    else:
                        cursor.execute(
                            """
                            SELECT user_id
                            FROM user_tokens 
                            WHERE provider = 'slack' 
                            AND subscription_id = %s
                            AND is_active = TRUE
                            """,
                            (team_id,)
                        )
                    results = cursor.fetchall()
                    if results:
                        return results
        except Exception as e:
            errors += 1
            logger.debug(f"Error querying slack tokens for org {org_id}: {e}")
            continue

    if errors == len(org_ids):
        logger.warning(f"All org lookups failed for Slack team {sanitize(team_id)} ({errors} errors)")
    return []


def get_user_id_from_slack_team(team_id: str) -> Optional[str]:
    """
    Find Aurora user_id from Slack team_id (workspace ID).
    Returns the first active user connected to this Slack workspace.
    Note: team_id is stored in the subscription_id column for Slack.

    This is to send a message into a channel where the requesting user is not authenticated. (We need credentials to send a message)
    """
    try:
        results = _lookup_slack_token(team_id)
        if results:
            return results[0][0]
        return None
    except Exception as e:
        logger.error(f"Error looking up user from Slack team_id {sanitize(team_id)}: {e}", exc_info=True)
        return None


def get_user_id_from_slack_user(slack_user_id: str, team_id: str) -> Optional[str]:
    """
    Find Aurora user_id from Slack user_id.
    Checks if the Slack user who @mentioned Aurora is authenticated in Aurora.
    Note: team_id is stored in the subscription_id column for Slack.
    """
    try:
        results = _lookup_slack_token(team_id, include_secret_ref=True)
        
        if not results:
            return None
        
        from utils.secrets.secret_ref_utils import get_user_token_data
        
        for user_id, secret_ref in results:
            try:
                token_data = get_user_token_data(user_id, "slack")
                if not token_data:
                    continue
                
                stored_slack_user_id = token_data.get('user_id')
                
                if stored_slack_user_id == slack_user_id:
                    return user_id
            except Exception as e:
                logger.debug(f"Error checking user {user_id}: {e}")
                continue
        
        return None
    except Exception as e:
        logger.error(f"Error looking up user from Slack user_id {sanitize(slack_user_id)}: {e}", exc_info=True)
        return None


def format_slack_speaker(slack_user_id: Optional[str], display_name: Optional[str], *, is_bot: bool = False) -> str:
    """Speaker label Slack can notify on. ``<@USERID>`` pings; a display name does not."""
    name = (display_name or "").strip()
    # Bots have no one to notify; keep the label as a name.
    if is_bot:
        return name or "Aurora"
    uid = (slack_user_id or "").strip()
    mention = f"<@{uid}>" if _SLACK_USER_ID.fullmatch(uid) else ""
    # Name is just the id we already printed — don't repeat it.
    if mention and name and name not in (uid, mention):
        return f"{mention} ({name})"
    return mention or name or "Unknown"


def format_asked_by(slack_user_id: Optional[str], display_name: Optional[str], message: str) -> str:
    """Prefix a Slack question with the asker's mention token."""
    # No Slack id — nothing to mention, so the question goes through as typed.
    if not slack_user_id:
        return message
    return f"Asked by {format_slack_speaker(slack_user_id, display_name)}: {message}"


def slack_session_title(message_text: str) -> str:
    """Title is the question. The Asked-by mention stays in the prompt only."""
    question = _ASKED_BY_PREFIX.sub("", message_text, count=1)
    body = question[:TITLE_MAX_LENGTH] + "..." if len(question) > TITLE_MAX_LENGTH else question
    return f"Slack: {body}"


def mention_bot_ids(authorizations, stored_bot_user_id: Optional[str] = None) -> list:
    """Bot ids to strip. Event authorizations first, then the id saved at install."""
    ids = [
        auth.get("user_id")
        for auth in (authorizations or [])
        if isinstance(auth, dict) and auth.get("is_bot") and auth.get("user_id")
    ]
    # Payload omitted the bot. The install still recorded it, so don't strip people.
    if not ids and stored_bot_user_id:
        return [stored_bot_user_id]
    return ids


def strip_bot_mentions(text: str, bot_user_ids: list) -> str:
    """Remove Aurora's own mention and keep every other <@USERID>."""
    if not text:
        return ""
    ids = {str(bid) for bid in bot_user_ids if bid and _SLACK_USER_ID.fullmatch(str(bid))}
    # Still no bot id — can't tell Aurora from a person, so drop every mention.
    if not ids:
        return _SLACK_MENTION.sub("", text).strip()

    def _keep_people(match):
        # Aurora's mention, with or without a |label. Everyone else stays.
        if match.group(1) in ids:
            return ""
        return match.group(0)

    return _SLACK_MENTION.sub(_keep_people, text).strip()


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


def get_workspace_member(client, slack_user_id: str, team_id: str) -> Optional[dict]:
    """Full member as {"display_name", "email"}, or None.

    Excludes deleted users, bots, Slack Connect, and guests. email is None
    when the workspace did not share one.
    """
    if not slack_user_id or not team_id:
        return None
    try:
        result = client._make_request("GET", "users.info", {"user": slack_user_id}) or {}
    except Exception as e:
        logger.warning(f"users.info failed for Slack user {sanitize(slack_user_id)}: {e}")
        return None

    user = result.get("user") or {}
    if not result.get("ok") or not user:
        return None
    if user.get("team_id") != team_id:
        return None  # Slack Connect / external workspace
    if user.get("deleted") or user.get("is_bot") or user.get("id") == "USLACKBOT":
        return None
    if user.get("is_stranger") or user.get("is_restricted") or user.get("is_ultra_restricted"):
        return None  # external user or guest account

    profile = user.get("profile") or {}
    display_name = (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("name")
        or slack_user_id
    )
    # users:read.email puts this on the profile. Hidden emails stay unset.
    email = (profile.get("email") or "").strip() or None
    return {"display_name": display_name.strip(), "email": email}


# Org preference written from the Slack manage page.
SLACK_ALLOW_UNREGISTERED_KEY = "slack_allow_unregistered"

_NO_ACCOUNT = (
    "Aurora couldn't match your Slack email to an account in this organization. "
    "Sign in to Aurora with that email, or ask an admin to allow messages from people without an account."
)
_NOT_A_MEMBER = "Aurora is only available to members of this Slack workspace."


def org_allows_unregistered_slack(org_id: str) -> bool:
    """Missing preference keeps workspace members able to ask. No org does not."""
    if not org_id:
        return False
    from utils.auth.stateless_auth import get_org_preference
    from utils.auth.viewer_run import flag_enabled

    # A workspace that has never touched the switch still lets members ask.
    value = get_org_preference(org_id, SLACK_ALLOW_UNREGISTERED_KEY, default=True)
    return flag_enabled(value, True)


def find_aurora_user_in_org(email: str, org_id: str) -> Optional[str]:
    """Aurora user in this org with this email. None when missing or ambiguous."""
    if not email or "@" not in email or not org_id:
        return None
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # users is not RLS-protected. Two case-variants and no exact
                # hit can be two people, so don't pick one.
                cursor.execute(
                    """SELECT id, email FROM users
                        WHERE LOWER(email) = LOWER(%s) AND org_id = %s
                        ORDER BY (email = %s) DESC, created_at ASC
                        LIMIT 2""",
                    (email, org_id, email),
                )
                rows = cursor.fetchall()
        if not rows:
            return None
        # Exact case is unambiguous. A lone case-insensitive row is too.
        if rows[0][1] != email and len(rows) > 1:
            logger.warning(
                "Ambiguous Aurora accounts for a Slack sender in org %s",
                sanitize(org_id),
            )
            return None
        return rows[0][0]
    except Exception:
        logger.warning("Slack email lookup failed for org %s", sanitize(org_id), exc_info=True)
        return None


def classify_slack_sender(
    *,
    matched_user_id: Optional[str],
    installer_user_id: Optional[str],
    is_full_member: bool,
    workspace_user_id: Optional[str],
    allow_unregistered: bool,
) -> tuple:
    """Who an @mention runs as: (user_id, as_general_viewer, refusal)."""
    # Slack email is an account in this org, so that role and team apply.
    if matched_user_id:
        return matched_user_id, False, None
    # The person who connected Slack, when the profile email doesn't match.
    if installer_user_id:
        return installer_user_id, False, None
    # Guest, external, or users.info failed. Don't guess a viewer.
    if not is_full_member or not workspace_user_id:
        return None, False, _NOT_A_MEMBER
    # No Aurora account. The manage-page toggle is the only way in.
    if not allow_unregistered:
        return None, False, _NO_ACCOUNT
    return workspace_user_id, True, None


def describe_slack_sender(slack_user_id: str, team_id: str) -> dict:
    """Workspace owner, installer match, profile, and the Aurora user for this email."""
    workspace_user_id = get_user_id_from_slack_team(team_id)
    installer_user_id = get_user_id_from_slack_user(slack_user_id, team_id)
    org_id = None
    member = None
    matched_user_id = None
    if workspace_user_id:
        from utils.auth.stateless_auth import get_org_id_for_user
        from connectors.slack_connector.client import get_slack_client_for_user

        org_id = get_org_id_for_user(workspace_user_id)
        client = get_slack_client_for_user(workspace_user_id)
        # Full members only. Guests and Slack Connect users stay None.
        if client:
            member = get_workspace_member(client, slack_user_id, team_id)
        email = (member or {}).get("email")
        if email and org_id:
            matched_user_id = find_aurora_user_in_org(email, org_id)
    return {
        "workspace_user_id": workspace_user_id,
        "installer_user_id": installer_user_id,
        "org_id": org_id,
        "member": member,
        "matched_user_id": matched_user_id,
    }


def aurora_user_for_slack_sender(slack_user_id: str, team_id: str) -> Optional[str]:
    """Aurora account for a button click. People without an account get none."""
    sender = describe_slack_sender(slack_user_id, team_id)
    return sender["matched_user_id"] or sender["installer_user_id"]


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


def _append_context_reply(context_parts: list, client, reply: dict, user_name_cache: dict,
                          total_length: int, max_total_length: int):
    """Append one thread reply. Returns the new length, and whether to stop."""
    reply_user = reply.get('user')
    reply_bot = reply.get('bot_id') is not None
    reply_name = _resolve_user_display_name(client, reply_user, reply_bot, user_name_cache)
    reply_speaker = format_slack_speaker(reply_user, reply_name, is_bot=reply_bot)
    reply_text = reply.get('text', '').strip()
    reply_time = _format_slack_timestamp(reply.get('ts', ''))

    # Keep the channel summary short; the full text is still in the thread.
    if len(reply_text) > COMMAND_DISPLAY_TRUNCATE_LENGTH:
        reply_text = reply_text[:COMMAND_DISPLAY_TRUNCATE_LENGTH] + "..."

    timestamp_prefix = f"[{reply_time}] " if reply_time else ""
    reply_line = f"    • {timestamp_prefix}{reply_speaker}: {reply_text}"
    # Budget spent — keep what we have and stop walking this thread.
    if total_length + len(reply_line) > max_total_length:
        logger.warning("Reached total context limit. Stopping at thread reply.")
        return total_length, True
    context_parts.append(reply_line)
    return total_length + len(reply_line), False


def _append_context_replies(context_parts: list, client, replies: list, user_name_cache: dict,
                            total_length: int, max_total_length: int) -> int:
    """Append replies until the context budget is spent. Returns the new length."""
    for reply in replies:
        total_length, stop = _append_context_reply(
            context_parts, client, reply, user_name_cache, total_length, max_total_length,
        )
        # Cap is spent. A later line is the same size, and skipping one leaves a hole.
        if stop:
            break
    return total_length


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
            speaker = format_slack_speaker(user_id, display_name, is_bot=is_bot)
            readable_time = _format_slack_timestamp(ts)
            timestamp_prefix = f"[{readable_time}] " if readable_time else ""
            
            # Add main message with timestamp
            main_msg = f"• {timestamp_prefix}{speaker}: {text}"
            
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
                            
                            total_length = _append_context_replies(
                                context_parts, client, first_3, user_name_cache,
                                total_length, max_total_length,
                            )
                            
                            separator = f"    ... ({len(thread_replies) - 6} more messages) ..."
                            if total_length + len(separator) <= max_total_length:
                                context_parts.append(separator)
                                total_length += len(separator)
                            
                            total_length = _append_context_replies(
                                context_parts, client, last_3, user_name_cache,
                                total_length, max_total_length,
                            )
                        else:
                            # Fewer than 6 replies, show all
                            thread_header = f"  └─ Thread ({len(thread_replies)} replies):"
                            if total_length + len(thread_header) > max_total_length:
                                logger.warning(f"Reached total context limit. Truncating thread context.")
                                break
                            
                            context_parts.append(thread_header)
                            total_length += len(thread_header)
                            
                            total_length = _append_context_replies(
                                context_parts, client, thread_replies, user_name_cache,
                                total_length, max_total_length,
                            )
                
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

    The lookup is org-wide: the incidents channel is shared by the org and a
    recurrence's completion lands in its anchor's thread, which may belong to
    another member. It is scoped to the caller's org twice — by RLS and by an
    explicit org_id predicate — because a Slack ts is only unique within a
    channel, so two workspaces can hold the same value. The incident's RCA
    session is only reused for its owner; anyone else gets a fresh session
    linked to the incident.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[SlackEvents:incident_by_ts]")
                if not org_id:
                    return None, None
                cursor.execute(
                    """
                    SELECT id, aurora_chat_session_id, user_id
                    FROM incidents
                    WHERE slack_message_ts = %s AND org_id = %s
                    LIMIT 1
                    """,
                    (slack_message_ts, org_id)
                )
                result = cursor.fetchone()

                if result:
                    incident_id = str(result[0])
                    is_owner = str(result[2]) == str(user_id)
                    session_id = str(result[1]) if result[1] and is_owner else None
                    logger.info(f"Found incident {incident_id} (session: {session_id}) from slack_message_ts {slack_message_ts}")
                    return incident_id, session_id
                
                return None, None
                
    except Exception as e:
        logger.error(f"Error looking up incident by slack_message_ts: {e}", exc_info=True)
        return None, None


def get_session_from_thread(user_id: str, channel_id: str, thread_ts: str):
    """
    Find the session_id associated with a Slack thread.
    First checks if thread_ts matches an incident notification message; its
    RCA session is reused only by the incident's owner. Otherwise looks up
    this user's own earlier chat session in the thread (a plain Slack chat,
    or their Q&A on someone else's incident).
    Returns (session_id, incident_id), where incident_id is set only when
    session_id is that incident's RCA session — so a non-owner's first
    mention on an incident thread returns (None, incident_id) and the caller
    starts a Q&A session about it. (None, None) if nothing matches.
    """
    try:
        # First, check if this thread is from an incident notification
        incident_id, session_id = get_incident_by_slack_message(user_id, thread_ts)
        if session_id:
            return session_id, incident_id
        
        # Otherwise, look up by thread_ts in chat session metadata
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix="[SlackEvents:session_by_thread]"):
                    return None, incident_id
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
                    if incident_id:
                        # This user's Q&A session on an incident they do not own.
                        return session_id, None
                    
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
                
                return None, incident_id
                
    except Exception as e:
        logger.error(f"Error looking up session from thread: {e}", exc_info=True)
        return None, None


def _resolve_channel_label(user_id: str, channel_id: str) -> str:
    """Return a human '#name (id)' label for a Slack channel so the agent knows
    which channel a message came from — critical for scoped directives like
    "in this channel". Prefers our registered slack_channels row (no API call),
    falls back to the bare id. Never raises."""
    if not channel_id:
        return "unknown"
    from services.channels import registry
    name = registry.get_channel_name(user_id, "slack", channel_id)
    # Registered channel — use its name so it matches routing/descriptions.
    # Unregistered or lookup failed — the id alone still anchors "this channel".
    return f"#{name} ({channel_id})" if name else channel_id


def send_message_to_aurora(user_id: str, message_text: str, channel: str, thread_ts: str = None, 
                           incident_id: str = None, session_id: str = None, context_messages: list = None,
                           channel_context: str | None = None, thinking_message_ts: str | None = None,
                           label_user_id: str | None = None, access_as: str | None = None,
                           slack_user_id: str | None = None):
    """
    Route a Slack message to Aurora's chat system.
    Uses background chat task to process the message.
    Creates a chat session in the database so it appears in chat history.
    """
    # Import here to avoid circular dependency with chat.background.task
    from chat.background.task import run_background_chat, create_background_chat_session
    
    try:
        # Only the incident's own RCA session (its owner continuing it) runs as
        # the incident's investigation. A fresh session is a Q&A about the
        # incident: linked for context through chat_sessions.incident_id like
        # the incident chat in the UI, but it must not re-run the RCA lifecycle
        # (re-link aurora_chat_session_id, re-summarize, re-dispatch actions).
        rca_incident_id = incident_id if session_id else None
        # Create session if not exists
        if not session_id:
            title = slack_session_title(message_text)
            
            # Trigger metadata for tracking. access_as is how a guest is checked.
            trigger_metadata = {
                "source": "slack",
                "channel": channel,
                "thread_ts": thread_ts,
            }
            if access_as:
                trigger_metadata["access_as"] = access_as
            if slack_user_id:
                trigger_metadata["slack_user_id"] = slack_user_id
            
            # Create the session in the database so it appears in chat history
            session_id = create_background_chat_session(
                user_id=user_id,
                title=title,
                trigger_metadata=trigger_metadata,
                incident_id=incident_id,
            )
        
        # Build context from thread messages or channel messages
        context_str = ""
        
        if context_messages:
            # Thread context - use all messages (already limited to 10 by get_thread_messages)
            context_str = "\n\n--- Recent conversation context from Slack thread ---\n"
            
            for i, msg in enumerate(context_messages, 1):
                display_name = msg.get('display_name', msg.get('user', 'Unknown'))
                speaker = format_slack_speaker(
                    msg.get('user'), display_name, is_bot=bool(msg.get('is_bot')),
                )
                text = msg.get('text', '').strip()
                timestamp = msg.get('timestamp', '')
                readable_time = _format_slack_timestamp(timestamp)
                timestamp_prefix = f"[{readable_time}] " if readable_time else ""
                context_str += f"{i}. {timestamp_prefix}{speaker}: {text}\n"
            
            context_str += "--- End of thread context ---\n"
        
        elif channel_context:
            # Channel context - includes recent messages and thread summaries
            context_str = f"\n\n--- Recent channel context (last 5 messages with thread summaries) ---\n{channel_context}\n--- End of channel context ---\n"
        
        # Prepare the message with context. Lead with an explicit channel identity
        # so the agent can resolve references like "this channel" and scope any
        # directive it saves to the right channel (matches routing/descriptions).
        # Channel rows belong to whoever connected Slack, not whoever asked.
        channel_label = _resolve_channel_label(label_user_id or user_id, channel)
        # Mention rules live in the Slack skill and system prompt, not this turn.
        channel_header = f"[Slack message from channel {channel_label}]\n\n"
        full_message = f"{channel_header}{message_text}{context_str}"
        
        # Trigger metadata for tracking and response
        trigger_metadata = {
            "source": "slack",
            "channel": channel,
            "thread_ts": thread_ts,
            "thinking_message_ts": thinking_message_ts,  # Store the thinking message timestamp
        }
        if access_as:
            trigger_metadata["access_as"] = access_as
        if slack_user_id:
            trigger_metadata["slack_user_id"] = slack_user_id
        
        # Launch background chat task
        # For Slack @mentions, link to incident but don't send "investigation started" notifications
        run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=full_message,
            trigger_metadata=trigger_metadata,
            provider_preference=None,  # Use default providers
            incident_id=rca_incident_id,
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
                # No RLS needed — incident_suggestions not RLS-protected
                cursor.execute(
                    """
                    SELECT id, title, description, type, risk, command
                    FROM incident_suggestions
                    WHERE incident_id = %s
                    ORDER BY 
                        CASE type
                            WHEN 'fix' THEN 1
                            WHEN 'mitigation' THEN 2
                            WHEN 'diagnostic' THEN 3
                            WHEN 'communication' THEN 4
                            ELSE 5
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

    eligible = [
        s for s in suggestions
        if s.get('command')
    ][:max_suggestions]
    if not eligible:
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

    for suggestion in eligible:
        # Validate required fields
        title = suggestion.get('title', 'Action')
        if not title:
            title = 'Action'

        is_high_risk = suggestion.get('risk') == 'high'
        command = suggestion.get('command', '')

        # Truncate command for display (Slack has limits)
        # Show more context - users can click "More details" for full command
        command_display = command[:COMMAND_FULL_DISPLAY_LENGTH] + '... (click More details for full command)' if len(command) > COMMAND_FULL_DISPLAY_LENGTH else command

        # Build compact text with just title and command (no description)
        risk_label = " ⚠️ _High Risk_" if is_high_risk else ""
        text = f"*{title}*{risk_label}\n`{command_display}`"
        if len(text) > SLACK_SECTION_TEXT_BUFFER:  # Leave buffer for Slack
            text = text[:SLACK_SECTION_TEXT_BUFFER] + "..."

        # Run button removed — handler still exists for previously-sent messages
        action_elements = []
        
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


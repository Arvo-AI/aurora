"""
Helper functions for Google Chat Events handling.
Mirrors the Slack events helpers for feature parity.
"""

import hmac
import logging
import os
import re
from typing import Optional, Tuple
from utils.db.connection_pool import db_pool
from datetime import datetime
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from flask import request as flask_request

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 50

GCHAT_MAX_MESSAGE_LENGTH = 4096
GCHAT_CARD_SECTION_TEXT_LIMIT = 2048
GCHAT_SAFE_MESSAGE_LENGTH = 3900
COMMAND_DISPLAY_TRUNCATE_LENGTH = 150
COMMAND_FULL_DISPLAY_LENGTH = 500


def verify_google_chat_request(request_data: dict) -> bool:
    """
    Verify that the request came from Google Chat.

    Two-layer verification:
    1. JWT Bearer token in the Authorization header, verified against
       Google's public keys with aud == GOOGLE_CHAT_PROJECT_NUMBER.
    2. Verification token in the event payload (shared secret).

    Both layers are attempted. If JWT verification succeeds, the request
    is trusted. Otherwise, falls back to the verification token check.
    Rejects the request if neither method can confirm authenticity.
    """
    project_number = os.getenv("GOOGLE_CHAT_PROJECT_NUMBER")
    auth_header = flask_request.headers.get("Authorization", "")

    if project_number and auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        try:
            id_token.verify_token(
                bearer_token,
                google_requests.Request(),
                audience=project_number,
                certs_url="https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com",
            )
            return True
        except Exception as e:
            logger.warning(f"Google Chat JWT verification failed: {e}")

    verification_token = os.getenv("GOOGLE_CHAT_VERIFICATION_TOKEN")
    if not verification_token:
        logger.error("GOOGLE_CHAT_VERIFICATION_TOKEN not configured and JWT verification unavailable — rejecting request")
        return False

    event_token = request_data.get("token", "")
    if not event_token:
        logger.warning("No token in Google Chat event payload")
        return False

    return hmac.compare_digest(event_token, verification_token)


def get_org_google_chat_credentials(sender_email: str) -> Optional[Tuple[str, str, str]]:
    """
    Find the org's Google Chat credentials by looking up the sender's Aurora account.

    Requires the sender to have a registered Aurora user with a matching email
    and an org that has Google Chat connected.

    Returns (connector_owner_user_id, org_id, sender_user_id) or None.
    """
    if not sender_email or "@" not in sender_email:
        logger.warning(f"Invalid sender email: {sender_email!r}")
        return None

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, org_id FROM users WHERE email = %s",
                    (sender_email,),
                )
                user_row = cursor.fetchone()
                if not user_row or not user_row[1]:
                    logger.warning(f"No Aurora user or org found for {sender_email}")
                    return None

                sender_user_id, org_id = user_row

                cursor.execute(
                    """
                    SELECT ut.user_id
                    FROM user_tokens ut
                    WHERE ut.provider = 'google_chat'
                    AND ut.org_id = %s
                    AND ut.is_active = TRUE
                    AND ut.secret_ref IS NOT NULL
                    LIMIT 1
                    """,
                    (org_id,),
                )
                token_row = cursor.fetchone()
                if not token_row:
                    return None
                return token_row[0], org_id, sender_user_id
    except Exception as e:
        logger.error(f"Error looking up org Google Chat credentials for {sender_email}: {e}", exc_info=True)
        return None


def _format_google_timestamp(ts_str: str) -> str:
    """Convert Google Chat timestamp (ISO 8601) to readable format."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, AttributeError):
        return ""


def get_thread_messages(
    client, space_name: str, thread_key: str,
    limit: int = 10, max_message_length: int = 5000, max_total_length: int = 50000,
):
    """
    Fetch messages from a Google Chat thread for context.
    Returns list of messages in chronological order.
    """
    try:
        messages = client.list_messages(space_name, page_size=limit)

        # Filter to the thread
        thread_messages = [
            m for m in messages
            if m.get("thread", {}).get("name", "").endswith(thread_key)
            or m.get("thread", {}).get("threadKey") == thread_key
        ]

        formatted = []
        total_length = 0

        for msg in reversed(thread_messages):
            text = msg.get("text", "")
            if not text.strip():
                continue

            if len(text) > max_message_length:
                text = text[:max_message_length] + f"\n... [truncated from {len(text)} chars]"

            if total_length + len(text) > max_total_length:
                break

            sender = msg.get("sender", {})
            display_name = sender.get("displayName", "Unknown")
            is_bot = sender.get("type") == "BOT"
            ts = msg.get("createTime", "")

            formatted.append({
                "user": sender.get("name", "unknown"),
                "display_name": "Aurora" if is_bot else display_name,
                "text": text,
                "timestamp": ts,
                "is_bot": is_bot,
            })
            total_length += len(text)

        return list(reversed(formatted))
    except Exception as e:
        logger.error(f"Error fetching thread messages: {e}", exc_info=True)
        return []


def get_space_context_with_threads(
    client, space_name: str, limit: int = 5, max_total_length: int = 50000,
):
    """
    Fetch recent space messages for context.
    Returns formatted string with recent messages and thread summaries.
    """
    try:
        messages = client.list_messages(space_name, page_size=limit)

        if not messages:
            return ""

        context_parts = []
        total_length = 0

        for msg in messages:
            text = msg.get("text", "").strip()
            if not text:
                continue

            sender = msg.get("sender", {})
            is_bot = sender.get("type") == "BOT"
            display_name = "Aurora" if is_bot else sender.get("displayName", "Unknown")
            ts = msg.get("createTime", "")
            readable_time = _format_google_timestamp(ts)
            timestamp_prefix = f"[{readable_time}] " if readable_time else ""

            if len(text) > COMMAND_DISPLAY_TRUNCATE_LENGTH:
                text = text[:COMMAND_DISPLAY_TRUNCATE_LENGTH] + "..."

            main_msg = f"• {timestamp_prefix}{display_name}: {text}"

            if total_length + len(main_msg) > max_total_length:
                break

            context_parts.append(main_msg)
            total_length += len(main_msg)

        return "\n".join(reversed(context_parts)) if context_parts else ""

    except Exception as e:
        logger.error(f"Error fetching space context: {e}", exc_info=True)
        return ""


def format_response_for_google_chat(text: str, max_length: int = GCHAT_SAFE_MESSAGE_LENGTH) -> str:
    """
    Format Aurora's response for Google Chat.
    Google Chat supports similar markdown to Slack: *bold*, _italic_, `code`.
    """
    if not text:
        return ""

    formatted = text.replace("\\n", "\n")
    formatted = formatted.replace("\x00", "")

    # Remove in-text citations
    formatted = re.sub(r"\[(\d+(?:,\s*\d+)*)\]", "", formatted)
    formatted = re.sub(r"\s+([.,;:!?])", r"\1", formatted)
    formatted = re.sub(r"  +", " ", formatted)

    # Convert markdown bold **text** → *text* (Google Chat uses single asterisks)
    formatted = re.sub(r"\*\*([^\*]+)\*\*", r"*\1*", formatted)
    # Convert markdown links [text](url) → <url|text>
    formatted = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"<\2|\1>", formatted)
    # Remove HTML tags
    formatted = re.sub(r"<(?!http|@|#)([^>]+)>", "", formatted)

    if len(formatted) > max_length:
        formatted = formatted[:max_length] + "\n\n...(message truncated)"

    return formatted


def extract_summary_section(text: str) -> str:
    """
    Extract the summary section from an investigation response.
    (Shared with Slack – identical logic.)
    """
    if not text:
        return ""

    end_markers = [
        "Suggested Next Steps",
        "Next Steps",
        "Recommendations",
        "Action Items",
        "Proposed Actions",
        "Remediation Steps",
    ]

    earliest_pos = len(text)
    for marker in end_markers:
        for prefix in [
            "", "\n", " ", "## ", "### ", "#### ",
            "\n## ", "\n### ", "\n#### ", "* ", "** ", "\n* ", "\n** ",
        ]:
            pos = text.find(prefix + marker)
            if pos != -1 and pos < earliest_pos:
                earliest_pos = pos

    if earliest_pos < len(text):
        return text[:earliest_pos].strip()

    paragraphs = text.split("\n\n")
    return "\n\n".join(paragraphs[:3]).strip()


def get_session_from_thread(
    space_name: str, thread_key: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Find the session_id associated with a Google Chat thread.
    Matches by space + thread so any user in the thread shares the session.
    Returns (session_id, incident_id) or (None, None).
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cs.id, i.id
                    FROM chat_sessions cs
                    LEFT JOIN incidents i ON i.aurora_chat_session_id = cs.id
                    WHERE (cs.ui_state->'triggerMetadata'->>'source') = 'google_chat'
                    AND (cs.ui_state->'triggerMetadata'->>'space_name') = %s
                    AND (cs.ui_state->'triggerMetadata'->>'thread_key') = %s
                    ORDER BY cs.created_at DESC
                    LIMIT 1
                    """,
                    (space_name, str(thread_key)),
                )
                result = cursor.fetchone()

                if result:
                    session_id = str(result[0])
                    incident_id = str(result[1]) if result[1] else None
                    return session_id, incident_id

                return None, None
    except Exception as e:
        logger.error(f"Error looking up session from thread: {e}", exc_info=True)
        return None, None


def send_message_to_aurora(
    user_id: str, message_text: str, space_name: str,
    thread_key: str = None, incident_id: str = None, session_id: str = None,
    context_messages: list = None, space_context: str = None,
    thinking_message_name: str = None,
):
    """Route a Google Chat message to Aurora's chat system (background task)."""
    from chat.background.task import run_background_chat, create_background_chat_session

    try:
        if not session_id:
            title = "Google Chat: " + (
                message_text[:TITLE_MAX_LENGTH] + "..."
                if len(message_text) > TITLE_MAX_LENGTH
                else message_text
            )
            trigger_metadata = {
                "source": "google_chat",
                "space_name": space_name,
                "thread_key": thread_key,
            }
            session_id = create_background_chat_session(
                user_id=user_id,
                title=title,
                trigger_metadata=trigger_metadata,
            )

        context_str = ""
        if context_messages:
            context_str = "\n\n--- Recent conversation context from Google Chat thread ---\n"
            for i, msg in enumerate(context_messages, 1):
                display_name = msg.get("display_name", msg.get("user", "Unknown"))
                text = msg.get("text", "").strip()
                ts = msg.get("timestamp", "")
                readable_time = _format_google_timestamp(ts)
                timestamp_prefix = f"[{readable_time}] " if readable_time else ""
                context_str += f"{i}. {timestamp_prefix}{display_name}: {text}\n"
            context_str += "--- End of thread context ---\n"
        elif space_context:
            context_str = (
                f"\n\n--- Recent space context (last 5 messages) ---\n"
                f"{space_context}\n--- End of space context ---\n"
            )

        full_message = f"{message_text}{context_str}"

        trigger_metadata = {
            "source": "google_chat",
            "space_name": space_name,
            "thread_key": thread_key,
            "thinking_message_name": thinking_message_name,
        }

        run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=full_message,
            trigger_metadata=trigger_metadata,
            provider_preference=None,
            incident_id=incident_id,
            send_notifications=False,
        )

        return True

    except Exception as e:
        logger.error(f"Error sending Google Chat message to Aurora: {e}", exc_info=True)
        return False


def get_incident_suggestions(incident_id: str):
    """Get runnable suggestions for an incident (shared with Slack)."""
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
                    (incident_id,),
                )
                return [
                    {
                        "id": row[0], "title": row[1], "description": row[2],
                        "type": row[3], "risk": row[4], "command": row[5],
                    }
                    for row in cursor.fetchall()
                ]
    except Exception as e:
        logger.error(f"Error fetching incident suggestions for {incident_id}: {e}", exc_info=True)
        return []


def build_suggestion_cards(incident_id: str, suggestions: list, max_suggestions: int = 5) -> list:
    """
    Build Google Chat Card v2 widgets for runnable suggestions.
    Returns a list of card sections with action buttons.
    """
    if not suggestions:
        return []

    sections = []

    for suggestion in suggestions[:max_suggestions]:
        if not suggestion.get("command"):
            continue

        title = suggestion.get("title", "Action")
        command = suggestion.get("command", "")
        command_display = (
            command[:COMMAND_FULL_DISPLAY_LENGTH] + "..."
            if len(command) > COMMAND_FULL_DISPLAY_LENGTH
            else command
        )

        sections.append({
            "header": title,
            "widgets": [
                {
                    "decoratedText": {
                        "text": f"<font color=\"#666666\"><code>{command_display}</code></font>",
                        "wrapText": True,
                    },
                },
                {
                    "buttonList": {
                        "buttons": [
                            {
                                "text": "Run",
                                "onClick": {
                                    "action": {
                                        "function": "run_suggestion",
                                        "parameters": [
                                            {"key": "incident_id", "value": str(incident_id)},
                                            {"key": "suggestion_id", "value": str(suggestion["id"])},
                                        ],
                                    },
                                },
                                "color": {
                                    "red": 0.13, "green": 0.59, "blue": 0.95, "alpha": 1,
                                },
                            },
                            {
                                "text": "More details",
                                "onClick": {
                                    "action": {
                                        "function": "suggestion_details",
                                        "parameters": [
                                            {"key": "incident_id", "value": str(incident_id)},
                                            {"key": "suggestion_id", "value": str(suggestion["id"])},
                                        ],
                                    },
                                },
                            },
                        ],
                    },
                },
            ],
        })

    return sections

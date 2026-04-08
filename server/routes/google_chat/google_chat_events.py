"""
Google Chat Events handler for Aurora.
Handles incoming messages with @Aurora mentions and interactive card clicks.

Google Chat sends events via HTTP POST to a configured endpoint.
Event types: MESSAGE, CARD_CLICKED, ADDED_TO_SPACE, REMOVED_FROM_SPACE.
"""

import logging
import re
from flask import Blueprint, request, jsonify
from connectors.google_chat_connector.client import get_chat_app_client
from utils.db.connection_pool import db_pool
from utils.auth.enforcer import get_user_roles_in_org
from routes.google_chat.google_chat_events_helpers import (
    verify_google_chat_request,
    get_org_google_chat_credentials,
    get_thread_messages,
    get_space_context_with_threads,
    get_session_from_thread,
    send_message_to_aurora,
)
from chat.background.task import run_background_chat

logger = logging.getLogger(__name__)

google_chat_events_bp = Blueprint("google_chat_events", __name__)


def _user_can_execute(user_id: str) -> bool:
    """Return True if the user has admin or editor role (can run commands)."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return False
                org_id = row[0]
        roles = get_user_roles_in_org(user_id, org_id)
        return any(r in ("admin", "editor") for r in roles)
    except Exception as e:
        logger.error(f"Failed to check execution permission for user {user_id}: {e}")
        return False


@google_chat_events_bp.route("/events", methods=["POST"])
def google_chat_events():
    """
    Google Chat interaction event endpoint.
    Handles MESSAGE, CARD_CLICKED, ADDED_TO_SPACE, REMOVED_FROM_SPACE.
    """
    try:
        data = request.get_json()

        if not verify_google_chat_request(data):
            logger.warning("Invalid Google Chat request")
            return jsonify({"error": "Invalid request"}), 403

        event_type = data.get("type")

        if event_type == "ADDED_TO_SPACE":
            space = data.get("space", {})
            space_type = space.get("spaceType", "")
            if space_type == "SPACE":
                return jsonify({
                    "text": (
                        "Thanks for adding me! I'm Aurora, your AI SRE assistant.\n\n"
                        "Mention @Aurora in any message to ask me questions about "
                        "your infrastructure, incidents, or anything else!"
                    ),
                })
            return jsonify({"text": "Hi! I'm Aurora, your AI SRE assistant."})

        if event_type == "REMOVED_FROM_SPACE":
            return jsonify({})

        if event_type == "MESSAGE":
            return _handle_message(data)

        if event_type == "CARD_CLICKED":
            return _handle_card_clicked(data)

        return jsonify({})

    except Exception as e:
        logger.error(f"Error handling Google Chat event: {e}", exc_info=True)
        return jsonify({"text": "Sorry, something went wrong."})


def _handle_message(data: dict):
    """Handle a MESSAGE event (user @mentions Aurora or sends a DM)."""
    try:
        message = data.get("message", {})
        space = data.get("space", {})
        user = data.get("user", {})

        space_name = space.get("name", "")
        text = message.get("argumentText", message.get("text", "")).strip()
        thread_name = message.get("thread", {}).get("name", "")
        thread_key = thread_name.split("/")[-1] if thread_name else message.get("name", "")
        sender_email = user.get("email", "")

        client = None
        response_text = None
        trigger_background = False
        user_id = None

        try:
            org_creds = get_org_google_chat_credentials(sender_email)
            if not org_creds:
                logger.warning(f"No org Google Chat credentials for {sender_email}")
                return jsonify({})

            connector_owner_id, org_id, user_id = org_creds
            client = get_chat_app_client()
            if not client:
                logger.error("Failed to create Chat client (no service account or user token)")
                return jsonify({})

            if not _user_can_execute(user_id):
                logger.warning(f"User {user_id} ({sender_email}) lacks permission to interact with Aurora")
                client.send_message(
                    space_name=space_name,
                    text="You don't have permission to use Aurora. Ask an admin or editor in your organization to upgrade your role.",
                    thread_key=thread_key,
                )
                return jsonify({})

            clean_msg = re.sub(r"@\S+", "", text).strip()

            if not clean_msg:
                response_text = (
                    "Hi! I'm Aurora, your AI SRE assistant.\n\n"
                    "You can ask me questions about your infrastructure, "
                    "incidents, or anything else!\n"
                    'For example: "@Aurora my pods are failing in production. '
                    'What\'s going on?"'
                )
            else:
                response_text = "Thinking..."
                trigger_background = True
                text = clean_msg

            if response_text:
                try:
                    sent_msg = client.send_message(
                        space_name=space_name,
                        text=response_text,
                        thread_key=thread_key,
                    )

                    if trigger_background and sent_msg:
                        msg_name = sent_msg.get("name")
                        is_thread_reply = (
                            message.get("thread", {}).get("name")
                            and message.get("thread", {}).get("name") != message.get("name")
                        )

                        incident_id = None
                        session_id = None
                        context_messages = []
                        space_context = None

                        if is_thread_reply:
                            session_id, incident_id = get_session_from_thread(
                                space_name, thread_key,
                            )
                            context_messages = get_thread_messages(
                                client, space_name, thread_key,
                            )
                        else:
                            space_context = get_space_context_with_threads(
                                client, space_name, limit=5,
                            )

                        logger.info(
                            f"Processing @Aurora mention in {space_name}, "
                            f"thread {thread_key}: {text[:100]}"
                        )

                        send_message_to_aurora(
                            user_id=user_id,
                            message_text=text,
                            space_name=space_name,
                            thread_key=thread_key,
                            incident_id=incident_id,
                            session_id=session_id,
                            context_messages=context_messages,
                            space_context=space_context,
                            thinking_message_name=msg_name,
                        )
                except Exception as e:
                    logger.error(f"Failed to send message to Google Chat: {e}")
                    try:
                        client.send_message(
                            space_name=space_name,
                            text="Sorry, something went wrong while processing your request.",
                            thread_key=thread_key,
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Error processing MESSAGE event: {e}", exc_info=True)

        return jsonify({})

    except Exception as e:
        logger.error(f"Error in _handle_message: {e}", exc_info=True)
        return jsonify({})


def _handle_card_clicked(data: dict):
    """Handle CARD_CLICKED events (button actions in card messages)."""
    try:
        action = data.get("action", {})
        function_name = action.get("actionMethodName", action.get("function", ""))
        parameters = {p["key"]: p["value"] for p in action.get("parameters", [])}

        user = data.get("user", {})
        space = data.get("space", {})
        sender_email = user.get("email", "")
        space_name = space.get("name", "")

        org_creds = get_org_google_chat_credentials(sender_email)
        if not org_creds:
            return jsonify({}), 200

        connector_owner_id, org_id, sender_user_id = org_creds

        if function_name == "run_suggestion":
            return _handle_run_suggestion(
                data=data,
                parameters=parameters,
                sender_email=sender_email,
                sender_user_id=sender_user_id,
                connector_owner_id=connector_owner_id,
                space_name=space_name,
            )

        if function_name == "suggestion_details":
            return _handle_suggestion_details(
                data=data,
                parameters=parameters,
                sender_email=sender_email,
                connector_owner_id=connector_owner_id,
                space_name=space_name,
            )

        return jsonify({"text": "Action received"})

    except Exception as e:
        logger.error(f"Error handling CARD_CLICKED: {e}", exc_info=True)
        return jsonify({"text": "Sorry, something went wrong."})


def _handle_run_suggestion(
    data: dict, parameters: dict, sender_email: str,
    sender_user_id: str, connector_owner_id: str, space_name: str,
) -> tuple:
    """Handle the 'Run Suggestion' button click."""
    try:
        clicker_user_id = sender_user_id

        incident_id = parameters.get("incident_id")
        suggestion_id = parameters.get("suggestion_id")

        if not incident_id or not suggestion_id:
            return jsonify({"text": "Invalid action format"}), 200

        if not _user_can_execute(clicker_user_id):
            logger.warning(f"User {clicker_user_id} ({sender_email}) lacks permission to run suggestions")
            return jsonify({"text": "You don't have permission to run commands. Ask an admin or editor in your organization."}), 200

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.command, s.title, s.risk, i.user_id,
                           i.aurora_chat_session_id, u.email
                    FROM incident_suggestions s
                    JOIN incidents i ON s.incident_id = i.id
                    LEFT JOIN users u ON i.user_id = u.id
                    WHERE s.id = %s AND s.incident_id = %s
                    """,
                    (suggestion_id, incident_id),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"text": "Suggestion not found"}), 200
                command, title, risk, incident_owner_id, chat_session_id, owner_email = row

        logger.info(f"User {clicker_user_id} executing suggestion: {title} ({risk} risk)")

        client = get_chat_app_client()
        message = data.get("message", {})
        thread_key = message.get("thread", {}).get("name", "").split("/")[-1]

        clicker_name = data.get("user", {}).get("displayName") or sender_email

        thinking_message_name = None
        if client and thread_key:
            try:
                cmd_display = command[:200] + "..." if len(command) > 200 else command
                result = client.send_message(
                    space_name=space_name,
                    text=(
                        f"Executing: *{title}*\n"
                        f"`{cmd_display}`\n\n"
                        f"_Running with {clicker_name}'s credentials..._"
                    ),
                    thread_key=thread_key,
                )
                if result:
                    thinking_message_name = result.get("name")
            except Exception as e:
                logger.error(f"Failed to send acknowledgment: {e}")

        if chat_session_id:
            run_background_chat.delay(
                user_id=clicker_user_id,
                session_id=chat_session_id,
                initial_message=f"Execute this command: {command}",
                trigger_metadata={
                    "source": "google_chat_button",
                    "space_name": space_name,
                    "thread_key": thread_key,
                    "thinking_message_name": thinking_message_name,
                    "suggestion_id": suggestion_id,
                    "incident_id": incident_id,
                    "clicker_name": clicker_name,
                    "suggestion_title": title,
                    "suggestion_command": command,
                },
                provider_preference=None,
                incident_id=incident_id,
                send_notifications=False,
                mode="agent",
            )
            return jsonify({"text": f"Executing: {title}"}), 200

        return jsonify({"text": "No chat session found for this incident"}), 200

    except Exception as e:
        logger.error(f"Error handling run_suggestion: {e}", exc_info=True)
        return jsonify({"text": "Failed to execute command. Please try again."}), 200


def _handle_suggestion_details(
    data: dict, parameters: dict, sender_email: str,
    connector_owner_id: str, space_name: str,
) -> tuple:
    """Handle the 'More details' button click (private message to clicker)."""
    try:
        incident_id = parameters.get("incident_id")
        suggestion_id = parameters.get("suggestion_id")
        if not incident_id or not suggestion_id:
            return jsonify({}), 200

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.title, s.description, s.command, s.type, s.risk
                    FROM incident_suggestions s
                    WHERE s.id = %s AND s.incident_id = %s
                    """,
                    (suggestion_id, incident_id),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({}), 200
                title, description, command, stype, risk = row

        client = get_chat_app_client()
        if client:
            try:
                max_cmd_len = 10000
                cmd_display = command
                if len(command) > max_cmd_len:
                    cmd_display = command[:max_cmd_len] + f"\n... [truncated from {len(command)} chars]"

                details_parts = [f"*{title}*"]
                if description:
                    details_parts.append(f"\n{description}")
                details_parts.append(f"\n*Full Command:*\n```{cmd_display}```")
                details_parts.append(f"\n*Type:* {stype}")
                details_parts.append(f"*Risk Level:* {risk}")

                user_name = data.get("user", {}).get("name", "")
                client.send_private_message(
                    space_name=space_name,
                    text="\n".join(details_parts),
                    user_name=user_name,
                )
            except Exception as e:
                logger.error(f"Failed to send details: {e}", exc_info=True)

        return jsonify({}), 200

    except Exception as e:
        logger.error(f"Error handling suggestion_details: {e}", exc_info=True)
        return jsonify({}), 200

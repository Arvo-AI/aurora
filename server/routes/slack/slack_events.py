"""
Slack Events API handler for Aurora.
Handles incoming messages with @Aurora mentions.
"""

import logging
import re
import json
import os
from flask import Blueprint, request, jsonify
from connectors.slack_connector.client import get_slack_client_for_user, SlackClient
from utils.db.connection_pool import db_pool
from routes.slack.slack_events_helpers import (
    verify_slack_signature,
    get_user_id_from_slack_user,
    get_user_id_from_slack_team,
    get_thread_messages,
    get_channel_context_with_threads,
    get_session_from_thread,
    send_message_to_aurora
)
from utils.secrets.secret_ref_utils import get_user_token_data
from chat.background.task import run_background_chat

logger = logging.getLogger(__name__)

slack_events_bp = Blueprint("slack_events", __name__)

# Get frontend URL from environment
FRONTEND_URL = os.getenv("FRONTEND_URL")


@slack_events_bp.route("/events", methods=["POST"])
def slack_events():
    """
    Slack Events API webhook endpoint.
    Handles @Aurora mentions and routes them to the chat system.
    """
    try:
        # Get raw request body for signature verification
        raw_body = request.get_data()
        timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
        signature = request.headers.get('X-Slack-Signature', '')
        
        # Verify request came from Slack
        if not verify_slack_signature(raw_body, timestamp, signature):
            logger.warning("Invalid Slack signature")
            return jsonify({"error": "Invalid signature"}), 403
        
        # Parse JSON payload
        data = request.get_json()
        
        # Handle Slack URL verification challenge
        if data.get('type') == 'url_verification':
            return jsonify({"challenge": data.get('challenge')})
        
        # Handle events
        if data.get('type') == 'event_callback':
            event = data.get('event', {})
            event_type = event.get('type')
            
            # Handle app_mention events (when someone @mentions Aurora)
            if event_type == 'app_mention':
                # Extract event details first
                channel = event.get('channel')
                ts = event.get('ts')  # Current message timestamp
                thread_ts = event.get('thread_ts') or event.get('ts')  # Parent thread timestamp
                slack_user_id = event.get('user')  # Slack user ID of person who @mentioned
                text = event.get('text', '')
                team_id = data.get('team_id')
                
                client = None
                response_text = None
                trigger_background = False
                user_id = None # Aurora user ID

                try:
                    # 1. Resolve Identity and Client
                    user_id = get_user_id_from_slack_user(slack_user_id, team_id)
                    
                    if user_id:
                        # User is connected and verified
                        client = get_slack_client_for_user(user_id)
                        if not client:
                            logger.error(f"Failed to create client for user {user_id}")
                    else:
                        # User not connected. Check if workspace is connected by anyone else.
                        workspace_user_id = get_user_id_from_slack_team(team_id)
                        if workspace_user_id:
                            # Use workspace credentials to send a helpful error message
                            token_data = get_user_token_data(workspace_user_id, "slack")
                            if token_data:
                                client = SlackClient(token_data.get('access_token'))
                                response_text = (
                                    "You're not authenticated in Aurora.\n\n"
                                    f"To use Aurora in this Slack workspace, please connect your Aurora account:\n{FRONTEND_URL}/settings/integrations\n\n"
                                    "Click 'Connect' for Slack and authorize this workspace."
                                )
                    
                    # 2. Logic processing (if no errors yet and client exists)
                    if client and not response_text:
                        # Remove @Aurora mention from text
                        clean_msg = re.sub(r'<@[A-Z0-9]+>', '', text).strip()
                        
                        if not clean_msg:
                            response_text = (
                                "Hi! I'm Aurora, your AI SRE assistant.\n\n"
                                "You can ask me questions about your infrastructure, incidents, or anything else!\n"
                                "For example: \"@Aurora my pods are failing in my production cluster. What's going on?\""
                            )
                        else:
                            response_text = "Thinking..."
                            trigger_background = True
                            # Update text to cleaned version for background task
                            text = clean_msg 

                    # 3. Execution
                    if client:
                        if response_text:
                            try:
                                sent_msg = client.send_message(
                                    channel=channel, 
                                    text=response_text, 
                                    thread_ts=thread_ts
                                )
                                
                                if trigger_background and sent_msg:
                                    # Proceed with background task
                                    # Determine session logic
                                    msg_thread_ts = event.get('thread_ts') # Use original thread_ts for logic
                                    
                                    # Logic for new session vs thread
                                    incident_id = None
                                    session_id = None
                                    context_messages = []
                                    channel_context = None
                                    final_thread_ts = None
                                    
                                    if msg_thread_ts and msg_thread_ts != ts:
                                         # Reply in thread
                                         session_id, incident_id = get_session_from_thread(user_id, channel, msg_thread_ts)
                                         context_messages = get_thread_messages(client, channel, msg_thread_ts)
                                         final_thread_ts = msg_thread_ts
                                    else:
                                         # New top level - fetch channel context with thread summaries
                                         channel_context = get_channel_context_with_threads(client, channel, limit=5)
                                         final_thread_ts = ts
                                    
                                    thinking_ts = sent_msg.get('ts')
                                    
                                    logger.info(f"Processing @Aurora mention in channel {channel}, thread {final_thread_ts}: {text[:100]}")
                                    
                                    send_message_to_aurora(
                                        user_id=user_id,
                                        message_text=text,
                                        channel=channel,
                                        thread_ts=final_thread_ts,
                                        incident_id=incident_id,
                                        session_id=session_id,
                                        context_messages=context_messages,
                                        channel_context=channel_context,
                                        thinking_message_ts=thinking_ts
                                    )
                            except Exception as e:
                                logger.error(f"Failed to send message to Slack: {e}")
                                # Try to inform user if main message failed
                                try:
                                    client.send_message(
                                        channel=channel, 
                                        text="Sorry, something went wrong while processing your request.", 
                                        thread_ts=thread_ts
                                    )
                                except:
                                    pass

                except Exception as e:
                    logger.error(f"Error processing app_mention: {e}", exc_info=True)
                    # Try to send error message if client available and we haven't sent a response yet
                    if client and not response_text:
                         try:
                             client.send_message(
                                 channel=channel, 
                                 text="Sorry, something went wrong processing your request.", 
                                 thread_ts=thread_ts
                             )
                         except:
                             pass
                
                return jsonify({"ok": True}), 200
        
        # Acknowledge other events
        return jsonify({"ok": True}), 200
        
    except Exception as e:
        logger.error(f"Error handling Slack event: {e}", exc_info=True)
        return jsonify({"ok": True}), 200  # Always acknowledge to Slack


@slack_events_bp.route("/interactions", methods=["POST"])
def slack_interactions():
    """
    Slack Interactive Components webhook endpoint.
    Handles button clicks, dropdowns, and other interactive elements.
    """
    try:
        # Get raw request body for signature verification
        raw_body = request.get_data()
        timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
        signature = request.headers.get('X-Slack-Signature', '')
        
        # Verify request came from Slack
        if not verify_slack_signature(raw_body, timestamp, signature):
            logger.warning("Invalid Slack signature on interaction")
            return jsonify({"error": "Invalid signature"}), 403
        
        # Parse payload (comes as form-encoded)
        payload_str = request.form.get('payload')
        if not payload_str:
            logger.error("No payload in interaction request")
            return jsonify({"error": "No payload"}), 400
        
        payload = json.loads(payload_str)
        
        # Extract key information
        interaction_type = payload.get('type')
        team_id = payload['team']['id']
        slack_user_id = payload['user']['id']
        channel_id = payload.get('channel', {}).get('id')
        
        logger.info(f"Received Slack interaction: type={interaction_type}, user={slack_user_id}, team={team_id}")
        
        # Handle button actions
        if interaction_type == 'block_actions':
            actions = payload.get('actions', [])
            if not actions:
                return jsonify({"text": "No action specified"}), 200
            
            action = actions[0]  # Handle first action
            action_id = action.get('action_id', '')
            
            # Handle "Run Suggestion" buttons
            if action_id.startswith('run_suggestion_'):
                return _handle_run_suggestion(
                    payload=payload,
                    action=action,
                    slack_user_id=slack_user_id,
                    team_id=team_id,
                    channel_id=channel_id
                )
            
            # Handle "More details" button
            if action_id.startswith('suggestion_details_'):
                return _handle_suggestion_details(
                    payload=payload,
                    action=action,
                    slack_user_id=slack_user_id,
                    team_id=team_id,
                    channel_id=channel_id
                )
        
        # Default response for unhandled interactions
        return jsonify({"text": "Interaction received"}), 200
        
    except Exception as e:
        logger.error(f"Error handling Slack interaction: {e}", exc_info=True)
        return jsonify({"text": "Sorry, something went wrong processing your action."}), 200


def _handle_run_suggestion(payload: dict, action: dict, slack_user_id: str, team_id: str, channel_id: str) -> tuple:
    """
    Handle the "Run Suggestion" button click.
    
    Security checks:
    1. Authenticate the clicker (map Slack user to Aurora user)
    2. Verify the clicker owns the incident/session
    3. Execute command with the clicker's credentials
    """
    try:
        # 1. AUTHENTICATE: Who clicked the button?
        clicker_user_id = get_user_id_from_slack_user(slack_user_id, team_id)
        
        if not clicker_user_id:
            logger.warning(f"Unauthenticated Slack user {slack_user_id} (team {team_id}) tried to run suggestion")
            
            # Try to send ephemeral message using workspace credentials
            workspace_user_id = get_user_id_from_slack_team(team_id)
            if workspace_user_id:
                try:
                    workspace_token_data = get_user_token_data(workspace_user_id, "slack")
                    if workspace_token_data:
                        workspace_client = SlackClient(workspace_token_data.get('access_token'))
                        workspace_client._make_request(
                            "POST",
                            "chat.postEphemeral",
                            {
                                "channel": channel_id,
                                "user": slack_user_id,
                                "text": f"WARNING: You're not authenticated in Aurora.\n\nTo run commands in this Slack workspace, connect your Aurora account:\n{FRONTEND_URL}/settings/integrations\n\nClick 'Connect' for Slack and authorize this workspace."
                            }
                        )
                        logger.info(f"Sent unauthenticated warning to Slack user {slack_user_id}")
                except Exception as e:
                    logger.error(f"Failed to send unauthenticated warning to Slack user {slack_user_id}: {e}", exc_info=True)
            
            return jsonify({"text": ""}), 200
        
        # 2. PARSE ACTION: Extract incident_id and suggestion_id
        value = action.get('value', '')  # Format: "incident_id:suggestion_id"
        if ':' not in value:
            logger.error(f"Invalid action value format: {value}")
            return jsonify({"text": "Invalid action format"}), 200
        
        incident_id, suggestion_id = value.split(':', 1)
        
        # 3. FETCH SUGGESTION from database
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Get suggestion details and owner info
                cursor.execute(
                    """
                    SELECT s.command, s.title, s.risk, i.user_id, i.aurora_chat_session_id, u.email
                    FROM incident_suggestions s
                    JOIN incidents i ON s.incident_id = i.id
                    LEFT JOIN users u ON i.user_id = u.id
                    WHERE s.id = %s AND s.incident_id = %s
                    """,
                    (suggestion_id, incident_id)
                )
                row = cursor.fetchone()
                
                if not row:
                    logger.error(f"Suggestion {suggestion_id} not found for incident {incident_id}")
                    return jsonify({"text": "WARNING: Suggestion not found"}), 200
                
                command, title, risk, incident_owner_id, chat_session_id, owner_email = row
        
        # 4. AUTHORIZE: Only incident owner can run commands
        owner_name = owner_email.split('@')[0] if owner_email else "the incident owner"
        
        if clicker_user_id != incident_owner_id:
            logger.warning(f"User {clicker_user_id} tried to run suggestion for incident owned by {incident_owner_id}")
            # Use ephemeral message for better UX
            client = get_slack_client_for_user(clicker_user_id)
            if client:
                try:
                    client._make_request(
                        "POST",
                        "chat.postEphemeral",
                        {
                            "channel": channel_id,
                            "user": slack_user_id,
                            "text": f"WARNING: Unauthorized\n\nThis investigation belongs to {owner_name}. Only the incident owner can run suggested commands."
                        }
                    )
                except Exception as e:
                    logger.error(f"Failed to send unauthorized message to Slack user {slack_user_id}: {e}", exc_info=True)
            
            return jsonify({"text": ""}), 200
        
        # 5. EXECUTE: Run the command with the clicker's credentials
        logger.info(f"User {clicker_user_id} executing suggestion: {title} ({risk} risk)")
        
        # Get Slack client for clicker to post updates
        client = get_slack_client_for_user(clicker_user_id)
        
        # Get thread_ts from the message (to reply in thread)
        message_ts = payload.get('message', {}).get('ts')
        
        # Get clicker's display name from Slack
        clicker_name = payload.get('user', {}).get('name', 'user')
        
        # Send immediate acknowledgment in thread
        thinking_message_ts = None
        if client and message_ts:
            try:
                result = client.send_message(
                    channel=channel_id,
                    text=f"Executing: *{title}*\n`{command[:200]}{'...' if len(command) > 200 else ''}`\n\n_Running with {clicker_name}'s credentials..._",
                    thread_ts=message_ts
                )
                if result:
                    thinking_message_ts = result.get('ts')
            except Exception as e:
                logger.error(f"Failed to send acknowledgment message: {e}")
        
        # Trigger background chat task to execute the command
        # Use agent mode for execution (not read-only)
        if chat_session_id:
            question = f"Execute this command: {command}"
            
            run_background_chat.delay(
                user_id=clicker_user_id,
                session_id=chat_session_id,
                initial_message=question,
                trigger_metadata={
                    "source": "slack_button",
                    "channel": channel_id,
                    "thread_ts": message_ts,
                    "thinking_message_ts": thinking_message_ts,  # Message to update with results
                    "suggestion_id": suggestion_id,
                    "incident_id": incident_id,
                },
                provider_preference=None,  # Use default providers
                incident_id=incident_id,
                send_notifications=False,
                mode="agent"  # AGENT MODE for execution
            )
            
            logger.info(f"Triggered execution of suggestion {suggestion_id} for user {clicker_user_id}")
            
            # Return success response (updates the button UI)
            return jsonify({
                "text": f"Executing: {title}\n\nResults will appear in the thread shortly."
            }), 200
        else:
            logger.error(f"No chat session found for incident {incident_id}")
            return jsonify({"text": "WARNING: No chat session found for this incident"}), 200
    
    except Exception as e:
        logger.error(f"Error handling run_suggestion action: {e}", exc_info=True)
        return jsonify({"text": "WARNING: Failed to execute command. Please try again."}), 200


def _handle_suggestion_details(payload: dict, action: dict, slack_user_id: str, team_id: str, channel_id: str) -> tuple:
    """
    Handle the "More details" button click.
    Shows the suggestion description as an ephemeral message (only visible to clicker).
    
    Authorization: Anyone authenticated to Aurora can view details (no ownership check).
    """
    try:
        # 1. AUTHENTICATE: Who clicked?
        clicker_user_id = get_user_id_from_slack_user(slack_user_id, team_id)
        
        if not clicker_user_id:
            logger.warning(f"Unauthenticated Slack user {slack_user_id} (team {team_id}) tried to view suggestion details")
            
            # Send ephemeral message using workspace credentials
            workspace_user_id = get_user_id_from_slack_team(team_id)
            if workspace_user_id:
                try:
                    workspace_token_data = get_user_token_data(workspace_user_id, "slack")
                    if workspace_token_data:
                        workspace_client = SlackClient(workspace_token_data.get('access_token'))
                        workspace_client._make_request(
                            "POST",
                            "chat.postEphemeral",
                            {
                                "channel": channel_id,
                                "user": slack_user_id,
                                "text": f"WARNING: You're not authenticated in Aurora.\n\nTo view details and run commands in this Slack workspace, connect your Aurora account:\n{FRONTEND_URL}/settings/integrations\n\nClick 'Connect' for Slack and authorize this workspace."
                            }
                        )
                except Exception as e:
                    logger.error(f"Failed to send unauthenticated warning to Slack user {slack_user_id}: {e}", exc_info=True)
            
            return jsonify({"text": ""}), 200
        
        # 2. NO OWNERSHIP CHECK - anyone authenticated can view details
        
        # 3. PARSE VALUE: Extract incident_id and suggestion_id
        value = action.get('value', '')
        
        if ':details' not in value:
            logger.error(f"Invalid details value format: {value}")
            return jsonify({"text": ""}), 200
        
        # Format: "incident_id:suggestion_id:details"
        parts = value.rsplit(':details', 1)[0]
        if ':' not in parts:
            return jsonify({"text": ""}), 200
        
        incident_id, suggestion_id = parts.split(':', 1)
        
        # 4. FETCH SUGGESTION DETAILS from database
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.title, s.description, s.command, s.type, s.risk
                    FROM incident_suggestions s
                    WHERE s.id = %s AND s.incident_id = %s
                    """,
                    (suggestion_id, incident_id)
                )
                row = cursor.fetchone()
                
                if not row:
                    logger.error(f"Suggestion {suggestion_id} not found")
                    return jsonify({"text": ""}), 200
                
                title, description, command, stype, risk = row
        
        # 5. SEND EPHEMERAL MESSAGE with details (no ownership check - anyone can view)
        client = get_slack_client_for_user(clicker_user_id)
        
        if client:
            try:
                # Build details message
                details_parts = [f"*{title}*"]
                
                if description:
                    details_parts.append(f"\n{description}")
                
                # Truncate extremely long commands (Slack ephemeral limit is ~40k chars)
                max_command_length = 10000
                if len(command) > max_command_length:
                    command_display = command[:max_command_length] + f"\n... [command truncated from {len(command)} to {max_command_length} characters]"
                else:
                    command_display = command
                
                details_parts.append(f"\n*Full Command:*\n```{command_display}```")
                details_parts.append(f"\n*Type:* {stype}")
                details_parts.append(f"*Risk Level:* {risk}")
                
                details_text = "\n".join(details_parts)
                
                # Send ephemeral message (only visible to clicker)
                client._make_request(
                    "POST",
                    "chat.postEphemeral",
                    {
                        "channel": channel_id,
                        "user": slack_user_id,
                        "text": details_text
                    }
                )
                
            except Exception as e:
                logger.error(f"Failed to send ephemeral details message to Slack user {slack_user_id}: {e}", exc_info=True)
        
        # Return empty response (interaction already handled)
        return jsonify({"text": ""}), 200
    
    except Exception as e:
        logger.error(f"Error handling suggestion_details action: {e}", exc_info=True)
        return jsonify({"text": ""}), 200
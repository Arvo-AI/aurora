"""
Slack notification service for sending incident alerts and updates.
Sends messages to the incidents channel in the user's connected Slack workspace.
"""

import logging
import os
from typing import Dict, Any, Optional
from datetime import datetime
from connectors.slack_connector.client import SlackClient, get_slack_client_for_user
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)

SLACK_MAX_BLOCKS = 50
FRONTEND_URL = os.getenv("FRONTEND_URL")
_DEFAULT_ALERT_TITLE = "Unknown Alert"


def _get_incidents_channel_id(user_id: str, client: SlackClient) -> Optional[str]:
    """
    Get the incidents channel ID. Checks stored credentials first,
    falls back to org preference (which survives disconnect/reconnect).
    """
    try:
        from utils.auth.stateless_auth import get_credentials_from_db, get_org_id_for_user, get_org_preference
        
        slack_creds = get_credentials_from_db(user_id, "slack")
        if slack_creds and slack_creds.get("incidents_channel_id"):
            return slack_creds["incidents_channel_id"]
        
        # Fallback: read from org preference
        org_id = get_org_id_for_user(user_id)
        if org_id:
            channel_id = get_org_preference(org_id, 'slack_incidents_channel_id')
            if channel_id:
                return channel_id
        
        logger.error(f"[SlackNotification] No Slack channel ID found for user {user_id}")
        return None
        
    except Exception:
        logger.exception("[SlackNotification] Error getting incidents channel ID")
        return None


def _format_timestamp(timestamp) -> str:
    """Format timestamp for display."""
    if isinstance(timestamp, datetime):
        return timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')
    return str(timestamp) if timestamp else 'just now'


def _get_incident_url(incident_id: str) -> str:
    """Get the full URL for an incident."""
    return f"{FRONTEND_URL}/incidents/{incident_id}"


def send_slack_investigation_started_notification(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """
    Send Slack notification when RCA investigation starts.
    
    Args:
        user_id: User ID to send notification to
        incident_data: Dictionary containing incident details
            - incident_id: UUID of the incident
            - alert_title: Alert title
            - severity: Alert severity
            - service: Affected service
            - source_type: Monitoring platform
            - started_at: Investigation start timestamp
            
    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return False
        
        channel_id = _get_incidents_channel_id(user_id, client)
        if not channel_id:
            logger.error(f"[SlackNotification] Could not find incidents channel for user {user_id}")
            return False
        
        # Extract incident data
        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', _DEFAULT_ALERT_TITLE)
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        source_type = incident_data.get('source_type', 'monitoring platform')
        started_at = incident_data.get('started_at')
        
        # Format data
        incident_url = _get_incident_url(incident_id)
        
        # Get owner information (same logic as completed notification)
        owner_name = "user"
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[SlackNotification:started]")
                    cursor.execute(
                        "SELECT email FROM users WHERE id = (SELECT user_id FROM incidents WHERE id = %s)",
                        (incident_id,)
                    )
                    owner_row = cursor.fetchone()
                    if owner_row and owner_row[0]:
                        owner_name = owner_row[0].split('@')[0]
        except Exception as e:
            logger.warning(f"[SlackNotification] Could not fetch owner name: {e}")

        # Build Slack message with blocks for better formatting
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Investigation Started"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_Investigation by {owner_name}_"
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Investigation"
                    },
                    "url": incident_url,
                    "style": "primary"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Alert:* {alert_title}\n*Severity:* {severity.title()}\n*Service:* {service}\n*Status:* In Progress\n\nAurora is analyzing this incident from {source_type}"
                }
            }
        ]
        
        # Validate blocks
        from routes.slack.slack_events_helpers import validate_slack_blocks
        if not validate_slack_blocks(blocks):
            logger.error(f"[SlackNotification] Block validation failed for 'started' notification")
            # Fallback to simple text
            simple_text = f"*Investigation Started*\n\n{alert_title}\n\nView: {incident_url}"
            result = client.send_message(channel=channel_id, text=simple_text)
            return result is not None
        
        # Send message
        result = client.send_message(
            channel=channel_id,
            text=f"Investigation Started: {alert_title}",  # Fallback text
            blocks=blocks
        )
        
        if result:
            # Store message timestamp in database for later updates
            message_ts = result.get('ts')
            if message_ts:
                try:
                    with db_pool.get_admin_connection() as conn:
                        with conn.cursor() as cursor:
                            set_rls_context(cursor, conn, user_id, log_prefix="[SlackNotification:storeTs]")
                            cursor.execute(
                                "UPDATE incidents SET slack_message_ts = %s WHERE id = %s",
                                (message_ts, incident_id)
                            )
                            conn.commit()
                except Exception as e:
                    logger.warning(f"[SlackNotification] Failed to store message timestamp: {e}", exc_info=True)
            
            logger.info(f"[SlackNotification] Sent 'started' notification for incident {incident_id}")
            return True
        else:
            logger.warning(f"[SlackNotification] Failed to send 'started' notification")
            return False
            
    except Exception:
        logger.exception("[SlackNotification] Error sending started notification")
        return False
    
def send_slack_investigation_completed_notification(
    user_id: str,
    incident_data: Dict[str, Any]
) -> bool:
    """
    Send Slack notification when RCA investigation completes.
    
    Args:
        user_id: User ID to send notification to
        incident_data: Dictionary containing incident details
            - incident_id: UUID of the incident
            - alert_title: Alert title
            - severity: Alert severity
            - service: Affected service
            - source_type: Monitoring platform
            - started_at: Investigation start timestamp
            - analyzed_at: Investigation completion timestamp
            - aurora_summary: RCA summary text
            - status: Incident status
            
    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return False
        
        channel_id = _get_incidents_channel_id(user_id, client)
        if not channel_id:
            logger.error(f"[SlackNotification] Could not find incidents channel for user {user_id}")
            return False
        
        # Extract incident data
        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', _DEFAULT_ALERT_TITLE)
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        started_at = incident_data.get('started_at')
        analyzed_at = incident_data.get('analyzed_at')
        aurora_summary = incident_data.get('aurora_summary') or 'Analysis in progress...'
        
        # Format data
        incident_url = _get_incident_url(incident_id)
        
        # Extract summary section (before "Suggested Next Steps") and format for Slack
        from routes.slack.slack_events_helpers import (
            format_response_for_slack, 
            extract_summary_section,
            get_incident_suggestions,
            build_suggestions_blocks
        )
        summary_only = extract_summary_section(aurora_summary)
        summary_for_slack = format_response_for_slack(summary_only)
        
        # Show the root cause conclusion paragraph (typically the 2nd paragraph)
        # rather than the "what happened" intro paragraph
        if not summary_for_slack:
            summary_for_slack = "Analysis completed. View full report for details."
        else:
            paragraphs = [p.strip() for p in summary_for_slack.split('\n\n') if p.strip()]
            if len(paragraphs) >= 2:
                # Use the 2nd paragraph (root cause conclusion) as it's the most valuable
                summary_for_slack = paragraphs[1]
            elif paragraphs:
                summary_for_slack = paragraphs[0]
            # Truncate if still too long
            if len(summary_for_slack) > 600:
                truncation_point = summary_for_slack.rfind(' ', 0, 600)
                if truncation_point < 200:
                    truncation_point = 600
                summary_for_slack = summary_for_slack[:truncation_point] + "..."
        
        # Get owner information
        owner_name = "user"
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[SlackNotification:completed]")
                    cursor.execute(
                        "SELECT email FROM users WHERE id = (SELECT user_id FROM incidents WHERE id = %s)",
                        (incident_id,)
                    )
                    owner_row = cursor.fetchone()
                    if owner_row and owner_row[0]:
                        owner_name = owner_row[0].split('@')[0]
        except Exception as e:
            logger.warning(f"[SlackNotification] Could not fetch owner name: {e}")
        
        # Build Slack message with blocks
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Analysis Complete"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_Investigation by {owner_name}_"
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Full Report"
                    },
                    "url": incident_url,
                    "style": "primary"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Alert:* {alert_title}\n*Severity:* {severity.title()}\n*Service:* {service}"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause Analysis:*\n{summary_for_slack}"
                }
            },
            {
                "type": "divider"
            }
        ]
        
        # Add the top suggestion (most valuable next step — fixes prioritized)
        try:
            suggestions = get_incident_suggestions(incident_id)
            if suggestions:
                suggestion_blocks = build_suggestions_blocks(incident_id, [suggestions[0]], max_suggestions=1)
                if suggestion_blocks:
                    blocks.extend(suggestion_blocks)
                    logger.info(f"[SlackNotification] Added top suggestion block for incident {incident_id}")
        except Exception as e:
            logger.warning(f"[SlackNotification] Failed to add suggestion block: {e}", exc_info=True)
        
        # Log the blocks for debugging
        logger.debug(f"[SlackNotification] Sending {len(blocks)} blocks to Slack")
        
        # Truncate blocks if exceeding Slack's limit (50 blocks max, use 45 for safety)
        if len(blocks) > SLACK_MAX_BLOCKS - 5:
            logger.warning(f"[SlackNotification] Truncating blocks from {len(blocks)} to {SLACK_MAX_BLOCKS - 5}")
            blocks = blocks[:SLACK_MAX_BLOCKS - 5]
        
        # Validate blocks before sending
        from routes.slack.slack_events_helpers import validate_slack_blocks
        if not validate_slack_blocks(blocks):
            logger.error(f"[SlackNotification] Block validation failed for incident {incident_id}")
            # Fallback to simple text message
            simple_text = f"*Analysis Complete*\n\n{alert_title}\n\nView full report: {incident_url}"
            result = client.send_message(
                channel=channel_id,
                text=simple_text
            )
            return result is not None
        
        result = client.send_message(
            channel=channel_id,
            text=f"Analysis Complete: {alert_title}",
            blocks=blocks
        )
        
        if result:
            return True
        else:
            return False
            
    except Exception:
        logger.exception("[SlackNotification] Error sending completed notification")
        return False


def send_slack_investigation_failed_notification(
    user_id: str,
    incident_data: Dict[str, Any],
    error_message: Optional[str] = None,
) -> bool:
    """
    Send Slack notification when RCA investigation fails.

    Args:
        user_id: User ID
        incident_data: Dictionary containing incident details
        error_message: Optional error description

    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return False

        channel_id = _get_incidents_channel_id(user_id, client)
        if not channel_id:
            return False

        incident_id = incident_data.get('incident_id', 'unknown')
        alert_title = incident_data.get('alert_title', _DEFAULT_ALERT_TITLE)
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')

        incident_url = _get_incident_url(incident_id)

        error_text = error_message or "The investigation encountered an error and could not complete."
        if len(error_text) > 300:
            error_text = error_text[:300] + "..."

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Investigation Failed"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_Aurora could not complete this investigation_"
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Incident"
                    },
                    "url": incident_url,
                    "style": "primary"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Alert:* {alert_title}\n*Severity:* {severity.title()}\n*Service:* {service}"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":x: *Error:* {error_text}"
                }
            }
        ]

        from routes.slack.slack_events_helpers import validate_slack_blocks
        if not validate_slack_blocks(blocks):
            simple_text = f"*Investigation Failed*\n\n{alert_title}\n\nError: {error_text}\n\nView incident: {incident_url}"
            result = client.send_message(channel=channel_id, text=simple_text)
            return result is not None

        result = client.send_message(
            channel=channel_id,
            text=f"Investigation Failed: {alert_title}",
            blocks=blocks
        )

        if result:
            logger.info(f"[SlackNotification] Sent 'failed' notification for incident {incident_id}")
            return True
        return False

    except Exception:
        logger.exception("[SlackNotification] Error sending failed notification")
        return False


def send_slack_action_started_notification(user_id: str, action_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Send Slack notification when an action starts running.

    Args:
        user_id: User ID
        action_data: Dictionary with action_name, run_id, session_id, started_at

    Returns:
        Dict with 'ts' and 'channel_id' if sent successfully, None otherwise
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return None

        channel_id = _get_incidents_channel_id(user_id, client)
        if not channel_id:
            return None

        action_name = action_data.get('action_name', 'Unknown Action')

        detail_text = f"*Action:* {action_name}\n*Status:* Running"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Action Started"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": detail_text
                }
            }
        ]

        from routes.slack.slack_events_helpers import validate_slack_blocks
        if not validate_slack_blocks(blocks):
            simple_text = f"*Action Started:* {action_name}"
            result = client.send_message(channel=channel_id, text=simple_text)
            if result and result.get('ts'):
                return {'ts': result['ts'], 'channel_id': channel_id}
            return None

        result = client.send_message(
            channel=channel_id,
            text=f"Action Started: {action_name}",
            blocks=blocks
        )

        if result and result.get('ts'):
            logger.info(f"[SlackNotification] Sent action started notification for '{action_name}'")
            return {'ts': result['ts'], 'channel_id': channel_id}
        return None

    except Exception:
        logger.exception("[SlackNotification] Error sending action started notification")
        return None


def _delete_start_message(client: SlackClient, action_data: Dict[str, Any], fallback_channel: str) -> None:
    """Delete the 'Action Started' message if its ts was stored."""
    start_msg_ts = action_data.get('start_message_ts')
    if not start_msg_ts:
        return
    channel = action_data.get('start_message_channel') or fallback_channel
    try:
        client.delete_message(channel=channel, ts=start_msg_ts)
        logger.info(f"[SlackNotification] Deleted action started message {start_msg_ts}")
    except Exception as e:
        logger.warning(f"[SlackNotification] Failed to delete started message: {e}")


def send_slack_action_completed_notification(user_id: str, action_data: Dict[str, Any]) -> bool:
    """
    Send Slack notification when an action completes (success or error).
    Deletes the "Action Started" message if one was stored.

    Args:
        user_id: User ID
        action_data: Dictionary with action_name, run_id, status, error, session_id, completed_at

    Returns:
        True if message sent successfully, False otherwise
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return False

        channel_id = _get_incidents_channel_id(user_id, client)
        if not channel_id:
            return False

        action_name = action_data.get('action_name', 'Unknown Action')
        status = action_data.get('status', 'unknown')
        error_message = action_data.get('error')
        result_summary = action_data.get('result_summary')
        session_id = action_data.get('session_id', '')
        session_url = f"{FRONTEND_URL}/chat?sessionId={session_id}" if session_id else None

        status_emoji = ":white_check_mark:" if status == 'success' else ":x:"
        status_text = "Completed Successfully" if status == 'success' else "Failed"

        detail_text = f"*Action:* {action_name}\n*Status:* {status_emoji} {status_text}"
        if result_summary:
            detail_text += f"\n*Result:* {result_summary}"
        if error_message:
            truncated_error = error_message[:200] + "..." if len(error_message) > 200 else error_message
            detail_text += f"\n*Error:* {truncated_error}"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Action Complete"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": detail_text
                }
            }
        ]

        if session_url:
            blocks[1]["accessory"] = {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "View Session"
                },
                "url": session_url,
            }

        from routes.slack.slack_events_helpers import validate_slack_blocks
        if not validate_slack_blocks(blocks):
            simple_text = f"*Action Complete:* {action_name} — {status_text}"
            result = client.send_message(channel=channel_id, text=simple_text)
            if result is not None:
                _delete_start_message(client, action_data, channel_id)
                return True
            return False

        result = client.send_message(
            channel=channel_id,
            text=f"Action Complete: {action_name} — {status_text}",
            blocks=blocks
        )

        if result:
            _delete_start_message(client, action_data, channel_id)
            logger.info(f"[SlackNotification] Sent action completed notification for '{action_name}' ({status})")
            return True
        return False

    except Exception:
        logger.exception("[SlackNotification] Error sending action completed notification")
        return False

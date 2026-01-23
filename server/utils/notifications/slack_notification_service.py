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

logger = logging.getLogger(__name__)

# Slack Block Kit blocks limit per message
SLACK_MAX_BLOCKS = 50
FRONTEND_URL = os.getenv("FRONTEND_URL")

# Slack Block Kit blocks limit per message
SLACK_MAX_BLOCKS = 50


def _get_incidents_channel_id(user_id: str, client: SlackClient) -> Optional[str]:
    """
    Get the incidents channel ID from stored credentials.
    """
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        
        slack_creds = get_credentials_from_db(user_id, "slack")
        if not slack_creds:
            logger.error(f"[SlackNotification] No Slack credentials found for user {user_id}")
            return None
        
        return slack_creds.get("incidents_channel_id")
        
    except Exception as e:
        logger.error(f"[SlackNotification] Error getting incidents channel ID: {e}", exc_info=True)
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
        alert_title = incident_data.get('alert_title', 'Unknown Alert')
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
            
    except Exception as e:
        logger.error(f"[SlackNotification] Error sending started notification: {e}", exc_info=True)
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
        alert_title = incident_data.get('alert_title', 'Unknown Alert')
        severity = incident_data.get('severity', 'unknown')
        service = incident_data.get('service', 'unknown')
        started_at = incident_data.get('started_at')
        analyzed_at = incident_data.get('analyzed_at')
        aurora_summary = incident_data.get('aurora_summary') or 'Analysis in progress...'
        slack_message_ts = incident_data.get('slack_message_ts')
        
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
        
        # Ensure summary is not empty and under Slack's 3000 char limit for section text
        if not summary_for_slack:
            summary_for_slack = "Analysis completed. View full report for details."
        elif len(summary_for_slack) > 2900:
            summary_for_slack = summary_for_slack[:2900] + "...\n\n(See full report for complete analysis)"
        
        # Get owner information
        owner_name = "user"
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
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
        
        # Add suggestion buttons if available
        try:
            suggestions = get_incident_suggestions(incident_id)
            if suggestions:
                logger.info(f"[SlackNotification] Found {len(suggestions)} suggestions for incident {incident_id}")
                suggestion_blocks = build_suggestions_blocks(incident_id, suggestions, max_suggestions=5)
                if suggestion_blocks:
                    blocks.extend(suggestion_blocks)
                    logger.info(f"[SlackNotification] Added {len(suggestion_blocks)} suggestion blocks")
        except Exception as e:
            logger.warning(f"[SlackNotification] Failed to add suggestion blocks: {e}", exc_info=True)
            # Continue without suggestions if they fail
        
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
        
        # Update existing message if timestamp exists, otherwise send new message
        if slack_message_ts:
            try:
                result = client.update_message(
                    channel=channel_id,
                    ts=slack_message_ts,
                    text=f"Analysis Complete: {alert_title}",  # Fallback text
                    blocks=blocks
                )
                if result and result.get('ok', False):
                    return True
                else:
                    logger.warning(f"[SlackNotification] Failed to update message, falling back to new message")
            except Exception as e:
                logger.warning(f"[SlackNotification] Error updating message, falling back to new message: {e}", exc_info=True)
        
        # Fallback: send new message if update failed or no timestamp exists
        result = client.send_message(
            channel=channel_id,
            text=f"Analysis Complete: {alert_title}",  # Fallback text
            blocks=blocks
        )
        
        if result:
            return True
        else:
            return False
            
    except Exception as e:
        logger.error(f"[SlackNotification] Error sending completed notification: {e}", exc_info=True)
        return False




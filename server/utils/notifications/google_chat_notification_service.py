"""
Google Chat notification service for sending incident alerts and updates.
Uses the Chat app service account so messages appear as "Aurora".
"""

import logging
import os
from typing import Dict, Any, Optional
from connectors.google_chat_connector.client import get_chat_app_client
from utils.db.connection_pool import db_pool
from routes.google_chat.google_chat_events_helpers import (
    format_response_for_google_chat,
    extract_summary_section,
    get_incident_suggestions,
    build_suggestion_cards,
)

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")


def _get_incidents_space_name(user_id: str) -> Optional[str]:
    """Get the incidents space name for the user's org."""
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        config = get_credentials_from_db(user_id, "google_chat")
        if not config:
            logger.error(f"[GChatNotification] No Google Chat config found for user {user_id}")
            return None
        return config.get("incidents_space_name")
    except Exception as e:
        logger.error(f"[GChatNotification] Error getting incidents space: {e}", exc_info=True)
        return None


def _get_chat_client(user_id: str):
    """Get the Chat app service account client."""
    return get_chat_app_client()


def _get_incident_url(incident_id: str) -> str:
    return f"{FRONTEND_URL}/incidents/{incident_id}"


def send_google_chat_investigation_started_notification(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Send Google Chat notification when RCA investigation starts."""
    try:
        client = _get_chat_client(user_id)
        if not client:
            return False

        space_name = _get_incidents_space_name(user_id)
        if not space_name:
            logger.error(f"[GChatNotification] No incidents space for user {user_id}")
            return False

        incident_id = incident_data.get("incident_id", "unknown")
        alert_title = incident_data.get("alert_title", "Unknown Alert")
        severity = incident_data.get("severity", "unknown")
        service = incident_data.get("service", "unknown")
        source_type = incident_data.get("source_type", "monitoring platform")
        incident_url = _get_incident_url(incident_id)

        owner_name = "unknown"
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT email FROM users WHERE id = (SELECT user_id FROM incidents WHERE id = %s)",
                        (incident_id,),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        owner_name = row[0]
        except Exception as e:
            logger.warning(f"[GChatNotification] Could not fetch owner name: {e}")

        cards_v2 = [
            {
                "cardId": f"investigation_started_{incident_id}",
                "card": {
                    "header": {
                        "title": "Investigation Started",
                        "subtitle": f"Investigation by {owner_name}",
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"decoratedText": {"topLabel": "Alert", "text": alert_title}},
                                {"decoratedText": {"topLabel": "Severity", "text": severity.title()}},
                                {"decoratedText": {"topLabel": "Service", "text": service}},
                                {"decoratedText": {"topLabel": "Status", "text": "In Progress"}},
                                {
                                    "decoratedText": {
                                        "text": f"Aurora is analyzing this incident from {source_type}",
                                    },
                                },
                            ],
                        },
                        {
                            "widgets": [
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "View Investigation",
                                                "onClick": {"openLink": {"url": incident_url}},
                                                "color": {
                                                    "red": 0.13, "green": 0.59,
                                                    "blue": 0.95, "alpha": 1,
                                                },
                                            },
                                        ],
                                    },
                                },
                            ],
                        },
                    ],
                },
            },
        ]

        fallback_text = f"Investigation Started: {alert_title}"

        try:
            result = client.send_message(
                space_name=space_name, text=fallback_text, cards_v2=cards_v2,
            )
        except Exception:
            logger.warning("[GChatNotification] Card send failed, falling back to plain text")
            result = client.send_message(space_name=space_name, text=fallback_text)

        if result:
            message_name = result.get("name")
            if message_name:
                try:
                    with db_pool.get_admin_connection() as conn:
                        with conn.cursor() as cursor:
                            cursor.execute(
                                "UPDATE incidents SET google_chat_message_name = %s WHERE id = %s",
                                (message_name, incident_id),
                            )
                            conn.commit()
                except Exception as e:
                    logger.warning(f"[GChatNotification] Failed to store message name: {e}")

            logger.info(f"[GChatNotification] Sent 'started' for incident {incident_id}")
            return True

        return False

    except Exception as e:
        logger.error(f"[GChatNotification] Error sending started notification: {e}", exc_info=True)
        return False


def send_google_chat_investigation_completed_notification(
    user_id: str, incident_data: Dict[str, Any],
) -> bool:
    """Send Google Chat notification when RCA investigation completes."""
    try:
        client = _get_chat_client(user_id)
        if not client:
            return False

        space_name = _get_incidents_space_name(user_id)
        if not space_name:
            logger.error(f"[GChatNotification] No incidents space for user {user_id}")
            return False

        incident_id = incident_data.get("incident_id", "unknown")
        alert_title = incident_data.get("alert_title", "Unknown Alert")
        severity = incident_data.get("severity", "unknown")
        service = incident_data.get("service", "unknown")
        aurora_summary = incident_data.get("aurora_summary") or "Analysis in progress..."
        google_chat_message_name = incident_data.get("google_chat_message_name")
        incident_url = _get_incident_url(incident_id)


        summary_only = extract_summary_section(aurora_summary)
        summary_formatted = format_response_for_google_chat(summary_only)

        if not summary_formatted:
            summary_formatted = "Analysis completed. View full report for details."
        elif len(summary_formatted) > 2900:
            summary_formatted = summary_formatted[:2900] + "...\n\n(See full report)"

        owner_name = "unknown"
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT email FROM users WHERE id = (SELECT user_id FROM incidents WHERE id = %s)",
                        (incident_id,),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        owner_name = row[0]
        except Exception as e:
            logger.warning(f"[GChatNotification] Could not fetch owner name: {e}")

        sections = [
            {
                "widgets": [
                    {"decoratedText": {"topLabel": "Alert", "text": alert_title}},
                    {"decoratedText": {"topLabel": "Severity", "text": severity.title()}},
                    {"decoratedText": {"topLabel": "Service", "text": service}},
                ],
            },
            {"header": "Root Cause Analysis", "widgets": [{"textParagraph": {"text": summary_formatted}}]},
            {
                "widgets": [
                    {
                        "buttonList": {
                            "buttons": [
                                {
                                    "text": "View Full Report",
                                    "onClick": {"openLink": {"url": incident_url}},
                                    "color": {"red": 0.13, "green": 0.59, "blue": 0.95, "alpha": 1},
                                },
                            ],
                        },
                    },
                ],
            },
        ]

        try:
            suggestions = get_incident_suggestions(incident_id)
            if suggestions:
                suggestion_sections = build_suggestion_cards(incident_id, suggestions)
                if suggestion_sections:
                    sections.extend(suggestion_sections)
        except Exception as e:
            logger.warning(f"[GChatNotification] Failed to add suggestions: {e}")

        cards_v2 = [
            {
                "cardId": f"analysis_complete_{incident_id}",
                "card": {
                    "header": {
                        "title": "Analysis Complete",
                        "subtitle": f"Investigation by {owner_name}",
                    },
                    "sections": sections,
                },
            },
        ]

        fallback_text = f"Analysis Complete: {alert_title}"

        # Try to update existing message, fallback to new message
        if google_chat_message_name:
            try:
                result = client.update_message(
                    message_name=google_chat_message_name,
                    text=fallback_text,
                    cards_v2=cards_v2,
                )
                if result:
                    return True
            except Exception as e:
                logger.warning(f"[GChatNotification] Failed to update message, sending new: {e}")

        try:
            result = client.send_message(
                space_name=space_name, text=fallback_text, cards_v2=cards_v2,
            )
        except Exception:
            result = client.send_message(space_name=space_name, text=fallback_text)

        return result is not None

    except Exception as e:
        logger.error(f"[GChatNotification] Error sending completed notification: {e}", exc_info=True)
        return False

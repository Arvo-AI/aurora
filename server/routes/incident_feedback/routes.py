"""API routes for incident feedback (Aurora Learn feature)."""

import logging
from flask import Blueprint, jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    get_user_preference,
    store_user_preference,
)
from routes.incident_feedback.weaviate_client import store_good_rca
from uuid import UUID

logger = logging.getLogger(__name__)

incident_feedback_bp = Blueprint("incident_feedback", __name__)

# Preference key for Aurora Learn toggle
AURORA_LEARN_PREFERENCE_KEY = "aurora_learn_enabled"

# Valid feedback types
VALID_FEEDBACK_TYPES = {"helpful", "not_helpful"}


def _validate_uuid(value: str) -> bool:
    """Validate that a string is a valid UUID."""
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def _is_aurora_learn_enabled(user_id: str) -> bool:
    """Check if Aurora Learn is enabled for a user. Defaults to True."""
    setting = get_user_preference(user_id, AURORA_LEARN_PREFERENCE_KEY, default=True)
    return setting is True or setting == "true"


# ============================================================================
# Feedback API
# ============================================================================


@incident_feedback_bp.route("/api/incidents/<incident_id>/feedback", methods=["POST"])
def submit_feedback(incident_id: str):
    """
    Submit feedback for an incident (thumbs up/down).

    POST body:
    {
        "feedback_type": "helpful" | "not_helpful",
        "comment": "optional comment for not_helpful"
    }
    """
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    # Check if Aurora Learn is enabled
    if not _is_aurora_learn_enabled(user_id):
        return jsonify({"error": "Aurora Learn is disabled. Enable it in Settings to provide feedback."}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    feedback_type = data.get("feedback_type")
    if feedback_type not in VALID_FEEDBACK_TYPES:
        return jsonify({
            "error": f"Invalid feedback_type. Must be one of: {', '.join(VALID_FEEDBACK_TYPES)}"
        }), 400

    comment = data.get("comment", "")
    if comment and len(comment) > 2000:
        return jsonify({"error": "Comment too long (max 2000 characters)"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Set RLS context
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))

                # Check if feedback already exists (feedback is final)
                cursor.execute(
                    """
                    SELECT id FROM incident_feedback
                    WHERE user_id = %s AND incident_id = %s
                    """,
                    (user_id, incident_id),
                )
                existing = cursor.fetchone()
                if existing:
                    return jsonify({
                        "error": "Feedback already submitted for this incident. Feedback cannot be changed."
                    }), 409

                # Verify the incident exists and belongs to the user
                cursor.execute(
                    """
                    SELECT id, aurora_status, aurora_summary, alert_title, alert_service,
                           source_type, severity
                    FROM incidents
                    WHERE id = %s AND user_id = %s
                    """,
                    (incident_id, user_id),
                )
                incident = cursor.fetchone()
                if not incident:
                    return jsonify({"error": "Incident not found"}), 404

                (
                    inc_id,
                    aurora_status,
                    aurora_summary,
                    alert_title,
                    alert_service,
                    source_type,
                    severity,
                ) = incident

                # Only allow feedback on completed analyses
                if aurora_status != "complete":
                    return jsonify({
                        "error": "Can only provide feedback on completed analyses"
                    }), 400

                # Save feedback to database (don't commit yet for helpful feedback)
                cursor.execute(
                    """
                    INSERT INTO incident_feedback (user_id, incident_id, feedback_type, comment)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (user_id, incident_id, feedback_type, comment or None),
                )
                feedback_row = cursor.fetchone()
                feedback_id = str(feedback_row[0])

                stored_for_learning = False

                # If helpful, store in Weaviate for future reference
                if feedback_type == "helpful":
                    # Fetch thoughts and citations
                    cursor.execute(
                        """
                        SELECT content, thought_type, timestamp
                        FROM incident_thoughts
                        WHERE incident_id = %s
                        ORDER BY timestamp ASC
                        """,
                        (incident_id,),
                    )
                    thought_rows = cursor.fetchall()
                    thoughts = [
                        {"content": row[0], "type": row[1], "timestamp": str(row[2])}
                        for row in thought_rows
                    ]

                    cursor.execute(
                        """
                        SELECT citation_key, tool_name, command, output
                        FROM incident_citations
                        WHERE incident_id = %s
                        ORDER BY citation_key
                        """,
                        (incident_id,),
                    )
                    citation_rows = cursor.fetchall()
                    citations = [
                        {
                            "key": row[0],
                            "tool_name": row[1],
                            "command": row[2],
                            "output": row[3][:500] if row[3] else "",  # Truncate output
                        }
                        for row in citation_rows
                    ]

                    # Store in Weaviate - if this fails, rollback DB transaction
                    stored_for_learning = store_good_rca(
                        user_id=user_id,
                        incident_id=incident_id,
                        feedback_id=feedback_id,
                        alert_title=alert_title or "",
                        alert_service=alert_service or "",
                        source_type=source_type or "",
                        severity=severity or "",
                        aurora_summary=aurora_summary or "",
                        thoughts=thoughts,
                        citations=citations,
                    )

                    if not stored_for_learning:
                        # Weaviate storage failed - rollback DB transaction
                        conn.rollback()
                        logger.error(
                            "[FEEDBACK] Weaviate storage failed, rolling back feedback for incident %s",
                            incident_id,
                        )
                        return jsonify({
                            "error": "Failed to store feedback for learning. Please try again."
                        }), 500

                # Commit DB transaction after Weaviate succeeds (or for not_helpful)
                conn.commit()

                logger.info(
                    "[FEEDBACK] User %s submitted %s feedback for incident %s (stored_for_learning=%s)",
                    user_id,
                    feedback_type,
                    incident_id,
                    stored_for_learning,
                )

                return jsonify({
                    "success": True,
                    "feedback_id": feedback_id,
                    "feedback_type": feedback_type,
                    "stored_for_learning": stored_for_learning,
                }), 201

    except Exception as exc:
        logger.exception("[FEEDBACK] Failed to submit feedback for incident %s", incident_id)
        return jsonify({"error": "Failed to submit feedback"}), 500


@incident_feedback_bp.route("/api/incidents/<incident_id>/feedback", methods=["GET"])
def get_feedback(incident_id: str):
    """Get feedback for an incident (if exists)."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))

                cursor.execute(
                    """
                    SELECT id, feedback_type, comment, created_at
                    FROM incident_feedback
                    WHERE user_id = %s AND incident_id = %s
                    """,
                    (user_id, incident_id),
                )
                row = cursor.fetchone()

                if not row:
                    return jsonify({"feedback": None}), 200

                feedback_id, feedback_type, comment, created_at = row

                return jsonify({
                    "feedback": {
                        "id": str(feedback_id),
                        "feedbackType": feedback_type,
                        "comment": comment,
                        "createdAt": created_at.isoformat() if created_at else None,
                    }
                }), 200

    except Exception as exc:
        logger.exception("[FEEDBACK] Failed to get feedback for incident %s", incident_id)
        return jsonify({"error": "Failed to get feedback"}), 500


# ============================================================================
# Aurora Learn Settings API
# ============================================================================


@incident_feedback_bp.route("/api/user/preferences/aurora-learn", methods=["GET"])
def get_aurora_learn_setting():
    """Get Aurora Learn setting for the current user."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    enabled = _is_aurora_learn_enabled(user_id)
    return jsonify({"enabled": enabled}), 200


@incident_feedback_bp.route("/api/user/preferences/aurora-learn", methods=["PUT"])
def set_aurora_learn_setting():
    """Update Aurora Learn setting for the current user."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    data = request.get_json()
    if data is None or "enabled" not in data:
        return jsonify({"error": "Missing 'enabled' field"}), 400

    enabled = data["enabled"]
    if not isinstance(enabled, bool):
        return jsonify({"error": "'enabled' must be a boolean"}), 400

    try:
        store_user_preference(user_id, AURORA_LEARN_PREFERENCE_KEY, enabled)
        logger.info("[AURORA LEARN] User %s set Aurora Learn to %s", user_id, enabled)
        return jsonify({"success": True, "enabled": enabled}), 200
    except Exception as exc:
        logger.exception("[AURORA LEARN] Failed to update setting for user %s", user_id)
        return jsonify({"error": "Failed to update setting"}), 500

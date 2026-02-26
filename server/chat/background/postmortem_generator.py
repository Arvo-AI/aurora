"""Generate AI-powered postmortems when an incident is resolved."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from celery_config import celery_app
from langchain_core.messages import HumanMessage

from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.llm import ModelConfig

logger = logging.getLogger(__name__)


def _extract_text_from_response(content: Union[str, List[Any]]) -> str:
    """Extract text content from LLM response, filtering out thinking blocks.

    Gemini thinking models return content as a list with thinking and text blocks.
    This extracts only the actual response text.
    """
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type", "")
                if part_type in ("thinking", "reasoning"):
                    continue
                elif part_type == "text":
                    text = part.get("text", "")
                    if text:
                        text_parts.append(str(text))
                else:
                    text = part.get("text", "")
                    if text:
                        text_parts.append(str(text))
            elif isinstance(part, str):
                text_parts.append(part)
        return "".join(text_parts).strip()

    return str(content).strip()


def _fetch_incident_for_postmortem(incident_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch incident data needed for postmortem generation."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT alert_title, alert_service, severity, aurora_summary,
                           started_at, analyzed_at, source_type
                    FROM incidents
                    WHERE id = %s AND user_id = %s AND status = 'resolved'
                    """,
                    (incident_id, user_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None

                (
                    alert_title,
                    alert_service,
                    severity,
                    aurora_summary,
                    started_at,
                    analyzed_at,
                    source_type,
                ) = row
                return {
                    "alert_title": alert_title or "Unknown Incident",
                    "alert_service": alert_service or "unknown",
                    "severity": severity or "unknown",
                    "aurora_summary": aurora_summary or "",
                    "started_at": started_at,
                    "analyzed_at": analyzed_at,
                    "source_type": source_type or "unknown",
                }
    except Exception as e:
        logger.error(f"[Postmortem] Failed to fetch incident {incident_id}: {e}")
        return None


def _fetch_incident_thoughts(incident_id: str) -> List[Dict[str, Any]]:
    """Fetch investigation thoughts for the incident."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT content, thought_type, timestamp
                    FROM incident_thoughts
                    WHERE incident_id = %s
                    ORDER BY timestamp ASC
                    LIMIT 50
                    """,
                    (incident_id,),
                )
                rows = cursor.fetchall()
                return [
                    {
                        "content": row[0] or "",
                        "thought_type": row[1] or "observation",
                        "timestamp": row[2],
                    }
                    for row in rows
                ]
    except Exception as e:
        logger.error(
            f"[Postmortem] Failed to fetch thoughts for incident {incident_id}: {e}"
        )
        return []


def _fetch_incident_suggestions(incident_id: str) -> List[Dict[str, Any]]:
    """Fetch suggestions for the incident."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT title, description, type
                    FROM incident_suggestions
                    WHERE incident_id = %s
                    """,
                    (incident_id,),
                )
                rows = cursor.fetchall()
                return [
                    {
                        "title": row[0] or "",
                        "description": row[1] or "",
                        "type": row[2] or "general",
                    }
                    for row in rows
                ]
    except Exception as e:
        logger.error(
            f"[Postmortem] Failed to fetch suggestions for incident {incident_id}: {e}"
        )
        return []


def _calculate_duration(
    started_at: Optional[datetime], analyzed_at: Optional[datetime]
) -> str:
    """Calculate human-readable duration between two timestamps."""
    if not started_at:
        return "Unknown"

    end_time = analyzed_at if analyzed_at else datetime.now()
    try:
        delta = end_time - started_at
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "Unknown"

        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if not parts:
            parts.append(f"{seconds}s")

        return " ".join(parts)
    except Exception:
        return "Unknown"


def _build_postmortem_prompt(
    incident: Dict[str, Any],
    thoughts: List[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
) -> str:
    """Build a prompt for LLM to generate a structured postmortem."""
    title = incident["alert_title"]
    service = incident["alert_service"]
    severity = incident["severity"]
    source_type = incident["source_type"]
    summary = incident["aurora_summary"]
    started_at = incident["started_at"]
    analyzed_at = incident["analyzed_at"]

    duration = _calculate_duration(started_at, analyzed_at)

    date_str = started_at.strftime("%Y-%m-%d %H:%M UTC") if started_at else "Unknown"

    # Format thoughts for the prompt
    thoughts_text = ""
    if thoughts:
        thought_lines = []
        for t in thoughts:
            ts = ""
            if t["timestamp"]:
                try:
                    ts = t["timestamp"].strftime("%H:%M:%S")
                except Exception:
                    ts = str(t["timestamp"])
            thought_type = t["thought_type"]
            content = t["content"]
            thought_lines.append(f"- [{ts}] ({thought_type}) {content}")
        thoughts_text = "\n".join(thought_lines)
    else:
        thoughts_text = "No investigation timeline data available."

    # Format suggestions for the prompt
    suggestions_text = ""
    if suggestions:
        suggestion_lines = []
        for s in suggestions:
            suggestion_lines.append(f"- [{s['type']}] {s['title']}: {s['description']}")
        suggestions_text = "\n".join(suggestion_lines)
    else:
        suggestions_text = "No suggestions available."

    prompt = f"""You are writing a postmortem document for an incident that has been resolved.
Generate a well-structured markdown postmortem based on the following incident data.

INCIDENT INFORMATION:
- Title: {title}
- Date: {date_str}
- Duration: {duration}
- Severity: {severity}
- Service: {service}
- Source: {source_type}

AURORA INVESTIGATION SUMMARY:
{summary if summary else "No investigation summary available."}

INVESTIGATION TIMELINE (thoughts from the AI investigation):
{thoughts_text}

SUGGESTED ACTIONS:
{suggestions_text}

Generate a postmortem in EXACTLY this markdown format:

# Postmortem: {title}

**Date:** {date_str}
**Duration:** {duration}
**Severity:** {severity}
**Service:** {service}
**Source:** {source_type}

## Summary
Write a brief 2-3 sentence description of what happened, based on the investigation summary.

## Timeline
Create a timeline of key events with timestamps from the investigation thoughts. Use the format:
- **HH:MM:SS** - Description of what happened
If no timestamps are available, describe the sequence of events.

## Root Cause
Based on the investigation summary, describe the root cause. If the root cause was not definitively identified, state what is known and what remains uncertain.

## Impact
Describe what was affected — services, users, data, SLAs, etc. Infer from the severity and service information.

## Resolution
Describe how the incident was resolved or mitigated, based on investigation findings.

## Action Items
Convert the suggested actions into actionable items. Format as:
- [ ] Action item description
If no suggestions are available, propose reasonable action items based on the root cause.

## Lessons Learned
Based on the root cause and investigation, describe what can be learned to prevent similar incidents.

RULES:
- Write in a professional, factual tone
- Do NOT speculate beyond what the data supports
- Do NOT include meta-commentary about the postmortem itself
- Keep the document concise but thorough
- Use the EXACT heading structure above
- Output ONLY the markdown document, no preamble or explanation
"""

    return prompt


def _save_postmortem(incident_id: str, user_id: str, content: str) -> None:
    """Save or update the postmortem in the database."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (incident_id)
                    DO UPDATE SET content = EXCLUDED.content,
                                  user_id = EXCLUDED.user_id,
                                  updated_at = CURRENT_TIMESTAMP
                    """,
                    (incident_id, user_id, content),
                )
                logger.info("[Postmortem] Upserted postmortem for incident %s", incident_id)
            conn.commit()
    except Exception as e:
        logger.error(
            f"[Postmortem] Failed to save postmortem for incident {incident_id}: {e}"
        )
        raise


@celery_app.task(
    bind=True,
    name="chat.background.generate_postmortem",
    time_limit=180,
    soft_time_limit=150,
    max_retries=2,
    default_retry_delay=10,
)
def generate_postmortem(self, incident_id: str, user_id: str) -> Dict[str, Any]:
    """Generate an AI-powered postmortem for a resolved incident.

    Fetches incident data, investigation thoughts, and suggestions, then
    uses an LLM to generate a structured markdown postmortem document.

    Args:
        incident_id: The incident ID to generate a postmortem for
        user_id: The user who triggered postmortem generation

    Returns:
        Dict with incident_id, status, and postmortem metadata
    """
    from celery.exceptions import SoftTimeLimitExceeded

    logger.info(
        f"[Postmortem] Generating postmortem for incident {incident_id} (user={user_id})"
    )

    try:
        # Fetch incident data
        incident = _fetch_incident_for_postmortem(incident_id, user_id)
        if not incident:
            logger.warning(
                f"[Postmortem] Incident {incident_id} not found; skipping postmortem generation"
            )
            return {"incident_id": incident_id, "status": "not_found"}

        # Fetch investigation thoughts and suggestions
        thoughts = _fetch_incident_thoughts(incident_id)
        suggestions = _fetch_incident_suggestions(incident_id)

        logger.info(
            f"[Postmortem] Fetched {len(thoughts)} thoughts and {len(suggestions)} suggestions for incident {incident_id}"
        )

        # Build the prompt
        prompt = _build_postmortem_prompt(incident, thoughts, suggestions)

        # Call LLM
        llm = create_chat_model(
            ModelConfig.EMAIL_REPORT_MODEL,
            temperature=0.3,
            streaming=False,
        )

        message = HumanMessage(content=prompt)
        response = llm.invoke([message])

        postmortem_content = (
            _extract_text_from_response(response.content)
            if response.content
            else "No postmortem generated"
        )

        logger.info(
            f"[Postmortem] Generated postmortem for incident {incident_id} ({len(postmortem_content)} chars)"
        )

        # Save to database
        _save_postmortem(incident_id, user_id, postmortem_content)

        return {
            "incident_id": incident_id,
            "status": "completed",
            "postmortem_length": len(postmortem_content),
        }

    except SoftTimeLimitExceeded:
        logger.error(
            f"[Postmortem] Timeout generating postmortem for incident {incident_id}"
        )
        return {
            "incident_id": incident_id,
            "status": "timeout",
        }

    except Exception as e:
        logger.exception(
            f"[Postmortem] Failed to generate postmortem for incident {incident_id}: {e}"
        )

        # Retry on transient errors
        if self.request.retries < self.max_retries:
            logger.info(
                f"[Postmortem] Retrying postmortem generation (attempt {self.request.retries + 1})"
            )
            raise self.retry(exc=e)

        return {
            "incident_id": incident_id,
            "status": "failed",
            "error": str(e),
        }

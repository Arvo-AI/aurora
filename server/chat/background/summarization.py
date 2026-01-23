"""Quick incident summarization for SREs using LLM."""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from celery_config import celery_app
from langchain_core.messages import HumanMessage

from chat.backend.agent.providers import create_chat_model


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


from chat.background.citation_extractor import (
    Citation,
    CitationExtractor,
    save_incident_citations,
)
from chat.background.suggestion_extractor import (
    SuggestionExtractor,
    save_incident_suggestions,
)

logger = logging.getLogger(__name__)


def _build_summary_prompt(
    source_type: str,
    alert_title: str,
    severity: str,
    service: str,
    raw_payload: Dict[str, Any],
    alert_metadata: Dict[str, Any],
) -> str:
    """Build a concise summary prompt for SRE consumption.

    Args:
        source_type: Alert source (grafana, netdata, datadog)
        alert_title: Alert title/name
        severity: Alert severity
        service: Affected service
        raw_payload: Raw webhook payload
        alert_metadata: Extracted metadata

    Returns:
        Prompt string for LLM summarization
    """
    # Extract key fields based on source
    key_details = []

    if source_type == "grafana":
        if alert_metadata.get("summary"):
            key_details.append(f"Summary: {alert_metadata['summary']}")
        if alert_metadata.get("description"):
            key_details.append(f"Description: {alert_metadata['description']}")
        if alert_metadata.get("labels"):
            labels = alert_metadata["labels"]
            key_details.append(f"Labels: {json.dumps(labels)}")

    elif source_type == "netdata":
        if alert_metadata.get("chart"):
            key_details.append(f"Chart: {alert_metadata['chart']}")
        if alert_metadata.get("value"):
            key_details.append(f"Current Value: {alert_metadata['value']}")
        if raw_payload.get("hostname"):
            key_details.append(f"Hostname: {raw_payload['hostname']}")

    elif source_type == "datadog":
        if alert_metadata.get("message"):
            key_details.append(f"Message: {alert_metadata['message']}")
        if alert_metadata.get("hostname"):
            key_details.append(f"Hostname: {alert_metadata['hostname']}")
        if alert_metadata.get("metric"):
            key_details.append(f"Metric: {alert_metadata['metric']}")

    elif source_type == "pagerduty":
        if alert_metadata.get("incidentId"):
            key_details.append(f"Incident ID: {alert_metadata['incidentId']}")
        if alert_metadata.get("urgency"):
            key_details.append(f"Urgency: {alert_metadata['urgency']}")
        if alert_metadata.get("priority"):
            key_details.append(f"Priority: {alert_metadata['priority']}")
        if alert_metadata.get("description"):
            key_details.append(f"Description: {alert_metadata['description']}")
        if alert_metadata.get("incidentUrl"):
            key_details.append(f"Incident URL: {alert_metadata['incidentUrl']}")
        if alert_metadata.get("customFields"):
            custom_fields = alert_metadata["customFields"]
            if isinstance(custom_fields, dict):
                for field_name, field_value in custom_fields.items():
                    key_details.append(f"{field_name}: {field_value}")

    details_text = (
        "\n".join(f"- {d}" for d in key_details)
        if key_details
        else "No additional details"
    )

    prompt = f"""
    You are rewriting an alert into a neutral incident summary.

ALERT INFORMATION:
- Source: {source_type}
- Title: {alert_title}
- Severity: {severity}
- Service: {service}

KEY DETAILS:
{details_text}

Write a concise 2–3 paragraph summary that:
- Describes what triggered the alert
- States the severity and observed impact (if explicitly present)
- Identifies the affected service or component
- States when the alert was triggered
- Includes only factual context present in the alert

STRICT RULES:
- Do NOT address any audience (do not mention SREs, engineers, teams)
- Do NOT give advice, recommendations, or next steps
- Do NOT explain what someone should do or be aware of
- Do NOT add conclusions such as “no action is required”
- Do NOT speculate beyond the alert content

Tone: neutral, factual, incident-record style
Style: descriptive, not advisory

"""

    return prompt


def _build_summary_prompt_with_chat(
    source_type: str,
    alert_title: str,
    severity: str,
    service: str,
    triggered_at: Optional[str],
    investigation_transcript: Optional[str] = None,
    citations: Optional[List[Citation]] = None,
) -> str:
    """Build a concise summary prompt that incorporates RCA chat context.

    If citations are provided, uses citation-based prompt with [n] markers.
    Otherwise falls back to transcript-based summarization.
    """
    triggered_line = f"- Triggered at: {triggered_at}" if triggered_at else ""

    # If we have citations, use citation-based prompt
    if citations:
        # Format evidence for prompt (limit to last 30 most recent citations)
        # Use last 30 to focus on the most recent investigation findings
        evidence_lines = []
        for c in citations[-30:]:
            # Skip citations without a valid index
            if c.index is None:
                logger.warning("[Summarization] Skipping citation with None index")
                continue

            # None-safe attribute access
            c_output = c.output if c.output else ""
            c_tool_name = c.tool_name if c.tool_name else "Unknown"
            c_command = c.command if c.command else "N/A"

            output_preview = c_output[:500] + "..." if len(c_output) > 500 else c_output
            output_preview = output_preview.replace("\n", " ").strip()
            evidence_lines.append(
                f"[{c.index}] {c_tool_name} - {c_command}\n    Output: {output_preview}"
            )

        evidence_text = "\n\n".join(evidence_lines)

        prompt = f"""You are writing an incident report based on alert data and forensic evidence.

ALERT INFORMATION:
- Source: {source_type}
- Title: {alert_title}
- Severity: {severity}
- Service: {service}
{triggered_line}

INVESTIGATION EVIDENCE (cite using [n] markers):
{evidence_text}

Write a 2-3 paragraph incident report:

PARAGRAPH 1 - What Happened:
State what occurred, when it occurred, and what was affected. Write as if you're reporting a known fact, not describing an investigation.
Example: "On [date], the data-processor service experienced OOMKilled events due to memory exhaustion [3, 5]."

PARAGRAPH 2 - Root Cause:
Directly state the root cause and explain the causal chain. Use evidence to support claims.
Example: "The root cause was a ConfigMap change that increased BATCH_SIZE from 1000 to 10000 [7], causing memory usage to exceed the 128Mi limit [9, 11]."

PARAGRAPH 3 (if significant) - Impact & Timeline:
Describe the scope of impact and any relevant timeline details.

CITATION RULES:
- Cite specific evidence that supports factual claims
- Group related citations together [3, 5, 7]
- Don't cite every detail - only key supporting evidence
- Never describe the investigation process or tools used
- Never say "Investigation revealed..." or "Attempts to..." - just state what happened

CRITICAL - DO NOT:
- Describe investigation steps or what tools were run
- Say "Investigation revealed" or "Attempts to query" or "The investigation was unable"
- Focus on tool failures or unavailable data
- Write about the RCA process itself

CRITICAL - DO:
- Write as if reporting a completed incident with known facts
- State the root cause directly in the first or second paragraph
- Focus on WHAT HAPPENED to the system, not HOW YOU FOUND OUT
- Use evidence citations to back up claims about system behavior

TONE: Professional, factual, incident-record style (not investigative process documentation)

After the summary, add a separate paragraph titled "## Suggested Next Steps" that:
- Lists 2-4 specific areas the SRE should investigate based on the findings
- Provides actionable guidance for further troubleshooting
- References specific metrics, logs, or infrastructure components mentioned in the investigation
- Keep it concise and targeted
"""
    else:
        # Fallback to transcript-based prompt
        transcript = investigation_transcript or "[No transcript available]"
        prompt = f"""
You are rewriting an alert plus the subsequent investigation transcript into a neutral incident summary.

ALERT INFORMATION:
- Source: {source_type}
- Title: {alert_title}
- Severity: {severity}
- Service: {service}
{triggered_line}

INVESTIGATION TRANSCRIPT (chat log):
{transcript}

Write a concise 2–3 paragraph summary that:
- Describes what triggered the alert
- States the severity and observed impact (if explicitly present)
- Identifies the affected service or component
- Summarizes investigation findings and best-known root cause (only if explicitly stated)
- If root cause is not explicit, state what is known and what is still uncertain

SUMMARY RULES:
- Do NOT address any audience in the summary paragraphs
- Tone: neutral, factual, incident-record style
- Style: descriptive, not advisory

After the summary, add a separate paragraph titled "## Suggested Next Steps" that:
- Lists 2-4 specific areas the SRE should investigate based on the findings
- Provides actionable guidance for further troubleshooting
- References specific metrics, logs, or infrastructure components mentioned in the investigation
- Keep it concise and targeted
"""
    return prompt


def _fetch_incident_basics(incident_id: str) -> Optional[Dict[str, Any]]:
    """Fetch minimal incident fields needed for chat-based summarization."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_type, alert_title, severity, alert_service, started_at
                    FROM incidents
                    WHERE id = %s
                    """,
                    (incident_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None

                source_type, alert_title, severity, alert_service, started_at = row
                triggered_at = None
                try:
                    triggered_at = started_at.isoformat() if started_at else None
                except Exception:
                    triggered_at = None

                return {
                    "source_type": source_type or "unknown",
                    "alert_title": alert_title or "",
                    "severity": severity or "unknown",
                    "service": alert_service or "unknown",
                    "triggered_at": triggered_at,
                }
    except Exception as e:
        logger.error(
            f"[IncidentSummary] Failed to fetch incident basics for {incident_id}: {e}"
        )
        return None


def _fetch_chat_transcript(
    user_id: str, session_id: str, max_chars: int = 12000
) -> str:
    """Fetch a chat session and format a compact transcript."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT messages
                    FROM chat_sessions
                    WHERE id = %s AND user_id = %s
                    """,
                    (session_id, user_id),
                )
                row = cursor.fetchone()
                if not row or row[0] is None:
                    return "[No transcript available]"

                messages = row[0]
                if isinstance(messages, str):
                    try:
                        messages = json.loads(messages)
                    except Exception:
                        return "[Transcript unavailable]"

                if not isinstance(messages, list):
                    return "[Transcript unavailable]"

                lines = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    sender = msg.get("sender") or msg.get("role") or ""
                    text = msg.get("text") or msg.get("content") or ""
                    if not text:
                        continue
                    if sender == "user":
                        prefix = "User"
                    elif sender in ("bot", "assistant"):
                        prefix = "Aurora"
                    else:
                        prefix = "Message"
                    lines.append(f"{prefix}: {str(text).strip()}")

                transcript = "\n".join(lines).strip()
                if not transcript:
                    return "[No transcript available]"

                if len(transcript) > max_chars:
                    transcript = "[Transcript truncated]\n" + transcript[-max_chars:]

                return transcript
    except Exception as e:
        logger.error(
            f"[IncidentSummary] Failed to fetch chat transcript for {session_id}: {e}"
        )
        return "[Transcript unavailable]"


@celery_app.task(
    bind=True,
    name="chat.background.generate_incident_summary",
    time_limit=120,  # 2 minute hard timeout
    soft_time_limit=90,  # 90 second soft timeout
    max_retries=2,
    default_retry_delay=10,
)
def generate_incident_summary(
    self,
    incident_id: str,
    user_id: str,
    source_type: str,
    alert_title: str,
    severity: str,
    service: str,
    raw_payload: Dict[str, Any],
    alert_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate a concise incident summary using LLM.

    This is a lightweight task that quickly summarizes an alert for SRE consumption.
    Unlike RCA background chats, this doesn't create a chat session or use tools.

    Args:
        incident_id: The incident ID to update
        user_id: User ID for logging
        source_type: Alert source (grafana, netdata, datadog)
        alert_title: Alert title
        severity: Alert severity
        service: Affected service
        raw_payload: Raw webhook payload
        alert_metadata: Extracted alert metadata

    Returns:
        Dict with incident_id, status, and summary
    """
    from celery.exceptions import SoftTimeLimitExceeded

    logger.info(
        f"[IncidentSummary] Generating summary for incident {incident_id} (user={user_id}, source={source_type})"
    )

    try:
        # Build the prompt
        prompt = _build_summary_prompt(
            source_type=source_type,
            alert_title=alert_title,
            severity=severity,
            service=service,
            raw_payload=raw_payload,
            alert_metadata=alert_metadata,
        )

        # Use Flash to generate summary (fast, no tool calls needed)
        # Uses provider-aware factory - will use direct Google API if configured, else OpenRouter
        llm = create_chat_model(
            "google/gemini-3-pro-preview",
            temperature=0.3,
            streaming=False,
        )

        message = HumanMessage(content=prompt)
        response = llm.invoke([message])

        summary = (
            _extract_text_from_response(response.content)
            if response.content
            else "No summary generated"
        )

        logger.info(
            f"[IncidentSummary] Generated summary for incident {incident_id} ({len(summary)} chars)"
        )

        # Update the incident with the summary
        # CRITICAL: Don't set aurora_status to 'complete' if RCA is running or pending
        # Only update the summary, preserve the current aurora_status
        _update_incident_summary(incident_id, summary, status=None)

        return {
            "incident_id": incident_id,
            "status": "completed",
            "summary_length": len(summary),
        }

    except SoftTimeLimitExceeded:
        logger.error(
            f"[IncidentSummary] Timeout generating summary for incident {incident_id}"
        )
        _update_incident_summary(
            incident_id,
            "Summary generation timed out. View raw alert for details.",
            status="error",
        )
        return {
            "incident_id": incident_id,
            "status": "timeout",
        }

    except Exception as e:
        logger.exception(
            f"[IncidentSummary] Failed to generate summary for incident {incident_id}: {e}"
        )

        # Retry on transient errors
        if self.request.retries < self.max_retries:
            logger.info(
                f"[IncidentSummary] Retrying summary generation (attempt {self.request.retries + 1})"
            )
            raise self.retry(exc=e)

        # After max retries, mark as failed
        _update_incident_summary(
            incident_id,
            "Summary generation failed. View raw alert for details.",
            status="error",
        )
        return {
            "incident_id": incident_id,
            "status": "failed",
            "error": str(e),
        }


@celery_app.task(
    bind=True,
    name="chat.background.generate_incident_summary_from_chat",
    time_limit=180,  # includes transcript fetch/format
    soft_time_limit=150,
    max_retries=2,
    default_retry_delay=10,
)
def generate_incident_summary_from_chat(
    self,
    incident_id: str,
    user_id: str,
    session_id: str,
) -> Dict[str, Any]:
    """Regenerate incident summary after RCA using the RCA chat transcript with citations."""
    from celery.exceptions import SoftTimeLimitExceeded

    logger.info(
        f"[IncidentSummary] Regenerating summary from chat for incident {incident_id} (user={user_id}, session={session_id})"
    )

    try:
        basics = _fetch_incident_basics(incident_id)
        if not basics:
            logger.warning(
                f"[IncidentSummary] Incident {incident_id} not found; skipping chat-based summary"
            )
            return {"incident_id": incident_id, "status": "not_found"}

        # Extract citations from the chat session (citations are simply tool calls and their outputs)
        extractor = CitationExtractor()
        all_citations = extractor.extract_citations_from_session(session_id, user_id)

        logger.info(
            f"[IncidentSummary] Extracted {len(all_citations)} potential citations for incident {incident_id}"
        )

        # Fetch transcript as fallback if no citations
        transcript = None
        if not all_citations:
            transcript = _fetch_chat_transcript(user_id=user_id, session_id=session_id)

        # Build prompt - uses citations if available, otherwise falls back to transcript
        prompt = _build_summary_prompt_with_chat(
            source_type=basics["source_type"],
            alert_title=basics["alert_title"],
            severity=basics["severity"],
            service=basics["service"],
            triggered_at=basics.get("triggered_at"),
            investigation_transcript=transcript,
            citations=all_citations if all_citations else None,
        )

        # Use Flash to generate summary (fast, no tool calls needed)
        # Uses provider-aware factory - will use direct Google API if configured, else OpenRouter
        llm = create_chat_model(
            "google/gemini-3-pro-preview",
            temperature=0.3,
            streaming=False,
        )

        message = HumanMessage(content=prompt)
        response = llm.invoke([message])
        summary = (
            _extract_text_from_response(response.content)
            if response.content
            else "No summary generated"
        )

        logger.info(
            f"[IncidentSummary] Generated chat-based summary for incident {incident_id} ({len(summary)} chars)"
        )

        # Parse used citations from the summary and save only those
        # Handles both single [1] and multi-citations [6, 7]
        used_citations = []
        if all_citations:
            # Find all citation blocks like [1], [6, 7], [4, 5, 6]
            citation_blocks = re.findall(r"\[(\d+(?:,\s*\d+)*)\]", summary)
            # Extract all individual numbers from the blocks
            used_keys = set()
            for block in citation_blocks:
                keys = re.findall(r"\d+", block)
                used_keys.update(keys)

            used_citations = [c for c in all_citations if str(c.index) in used_keys]
            if used_citations:
                save_incident_citations(incident_id, used_citations)
                logger.info(
                    f"[IncidentSummary] Saved {len(used_citations)} used citations for incident {incident_id}"
                )

        # Extract and save structured suggestions with commands
        try:
            suggestion_extractor = SuggestionExtractor()
            suggestions = suggestion_extractor.extract_suggestions(
                incident_id=incident_id,
                summary=summary,
                citations=used_citations if used_citations else all_citations,
                service=basics["service"],
                alert_title=basics["alert_title"],
            )
            if suggestions:
                save_incident_suggestions(incident_id, suggestions)
                logger.info(
                    f"[IncidentSummary] Saved {len(suggestions)} suggestions for incident {incident_id}"
                )
        except Exception as e:
            logger.exception(
                f"[IncidentSummary] Failed to extract suggestions for incident {incident_id}: {e}"
            )

        _update_incident_summary(incident_id, summary)

        # Send completion notifications now that summary is generated
        from utils.flags.feature_flags import is_slack_enabled
        from chat.background.task import (
            _send_rca_notification,
            _is_rca_email_notification_enabled,
            _has_slack_connected,
        )

        email_enabled = _is_rca_email_notification_enabled(user_id)
        slack_enabled = is_slack_enabled() and _has_slack_connected(user_id)

        if email_enabled or slack_enabled:
            _send_rca_notification(
                user_id,
                incident_id,
                "completed",
                email_enabled=email_enabled,
                slack_enabled=slack_enabled,
                session_id=session_id,
            )

        return {
            "incident_id": incident_id,
            "status": "completed",
            "summary_length": len(summary),
            "citations_count": len(used_citations),
        }

    except SoftTimeLimitExceeded:
        logger.error(
            f"[IncidentSummary] Timeout generating chat-based summary for incident {incident_id}"
        )
        _update_incident_summary(
            incident_id,
            "Summary generation timed out. View investigation chat for details.",
            status="error",
        )
        return {
            "incident_id": incident_id,
            "status": "timeout",
        }

    except Exception as e:
        logger.exception(
            f"[IncidentSummary] Failed chat-based summary for incident {incident_id}: {e}"
        )

        if self.request.retries < self.max_retries:
            logger.info(
                f"[IncidentSummary] Retrying chat-based summary (attempt {self.request.retries + 1})"
            )
            raise self.retry(exc=e)

        _update_incident_summary(
            incident_id,
            "Summary generation failed. View investigation chat for details.",
            status="error",
        )
        return {
            "incident_id": incident_id,
            "status": "failed",
            "error": str(e),
        }


def _update_incident_summary(
    incident_id: str, summary: str, status: Optional[str] = "complete"
) -> None:
    """Update the aurora_summary field for an incident.

    Args:
        incident_id: The incident ID (UUID format)
        summary: The generated summary text
        status: The aurora_status to set ('complete', 'error', etc.). If None, preserve current status.
    """
    from utils.db.connection_pool import db_pool
    from uuid import UUID
    from typing import Optional

    try:
        UUID(incident_id)  # Validate UUID format
        
        # Check current status before updating
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT status, aurora_status FROM incidents WHERE id = %s", (incident_id,))
                row = cursor.fetchone()
                current_aurora_status = None
                if row:
                    current_aurora_status = row[1]
                
                # If status is None, only update summary and preserve current aurora_status
                if status is None:
                    cursor.execute(
                        """
                        UPDATE incidents 
                        SET aurora_summary = %s, 
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (summary, datetime.now(), incident_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE incidents 
                        SET aurora_summary = %s, 
                            aurora_status = %s,
                            analyzed_at = CASE WHEN analyzed_at IS NULL THEN %s ELSE analyzed_at END,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (summary, status, datetime.now(), datetime.now(), incident_id),
                    )
                
                rows_updated = cursor.rowcount
            conn.commit()

            if rows_updated > 0:
                logger.info(
                    f"[IncidentSummary] Updated incident {incident_id} with summary (status={status})"
                )
            else:
                logger.warning(
                    f"[IncidentSummary] No rows updated for incident {incident_id}"
                )

    except Exception as e:
        logger.error(
            f"[IncidentSummary] Failed to update incident {incident_id} summary: {e}"
        )

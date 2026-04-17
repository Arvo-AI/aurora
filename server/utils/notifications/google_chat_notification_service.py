"""
Google Chat notification service — incident alerts and routing.

Sends two notifications per incident lifecycle:
  1. "Investigation Started" — when Aurora begins analyzing an incident
  2. "Analysis Complete"    — when the RCA is ready

Routing runs at both phases. The completion phase has more context (the RCA
summary), so it may route to different teams than the initial alert. If a team
was notified at start but isn't relevant anymore, their card is replaced with
an "Incident Re-routed" notice.
"""

import json
import logging
import os
from typing import Dict, Any, Optional, List

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

TEAM_ROUTING_PROMPT = (
    "You are an incident routing engine.\n\n"
    "Available teams:\n{teams}\n\n"
    "Routing instructions:\n{instructions}\n\n"
    "Incident context:\n{context}\n\n"
    "Which team(s) should be notified? Reply in this exact format:\n"
    "TEAMS: <comma-separated team names, or NONE>\n"
    "REASON: <one sentence explaining why these teams were chosen>"
)

BUTTON_COLOR = {"red": 0.13, "green": 0.59, "blue": 0.95, "alpha": 1}


# ── Card builders ───────────────────────────────────────────────────────
# Each function builds a complete Google Chat Card v2 payload.


def _build_started_card(incident_id: str, incident_data: Dict[str, Any], team_names: List[str]) -> tuple:
    """Build the "Investigation Started" card shown when Aurora begins analysis."""
    title = incident_data.get("alert_title", "Unknown Alert")
    severity = (incident_data.get("severity") or "unknown").title()
    service = incident_data.get("service") or "unknown"
    source = incident_data.get("source_type", "monitoring platform")
    teams_display = ", ".join(team_names) or "—"
    url = f"{FRONTEND_URL}/incidents/{incident_id}"

    cards_v2 = [{
        "cardId": f"investigation_started_{incident_id}",
        "card": {
            "header": {
                "title": "Investigation Started",
            },
            "sections": [
                {
                    "widgets": [
                        {"decoratedText": {"topLabel": "Alert", "text": title}},
                        {"decoratedText": {"topLabel": "Severity", "text": severity}},
                        {"decoratedText": {"topLabel": "Service", "text": service}},
                        {"decoratedText": {"topLabel": "Status", "text": "In Progress"}},
                        {"decoratedText": {"topLabel": "Teams notified", "text": teams_display}},
                        {"decoratedText": {"text": f"Aurora is analyzing this incident from {source}"}},
                    ],
                },
                {
                    "widgets": [{
                        "buttonList": {
                            "buttons": [{
                                "text": "View Investigation",
                                "onClick": {"openLink": {"url": url}},
                                "color": BUTTON_COLOR,
                            }],
                        },
                    }],
                },
            ],
        },
    }]

    fallback_text = f"Investigation Started: {title}"
    return cards_v2, fallback_text


def _build_completed_card(incident_id: str, incident_data: Dict[str, Any], team_names: List[str]) -> tuple:
    """Build the "Analysis Complete" card shown when the RCA is ready."""
    title = incident_data.get("alert_title", "Unknown Alert")
    severity = (incident_data.get("severity") or "unknown").title()
    service = incident_data.get("service") or "unknown"
    teams_display = ", ".join(team_names) or "—"
    url = f"{FRONTEND_URL}/incidents/{incident_id}"

    summary_raw = incident_data.get("aurora_summary") or "Analysis in progress..."
    summary = format_response_for_google_chat(extract_summary_section(summary_raw))
    if not summary:
        summary = "Analysis completed. View full report for details."
    elif len(summary) > 2900:
        summary = summary[:2900] + "...\n\n(See full report)"

    sections = [
        {
            "widgets": [
                {"decoratedText": {"topLabel": "Alert", "text": title}},
                {"decoratedText": {"topLabel": "Severity", "text": severity}},
                {"decoratedText": {"topLabel": "Service", "text": service}},
                {"decoratedText": {"topLabel": "Teams notified", "text": teams_display}},
            ],
        },
        {
            "header": "Root Cause Analysis",
            "widgets": [{"textParagraph": {"text": summary}}],
        },
        {
            "widgets": [{
                "buttonList": {
                    "buttons": [{
                        "text": "View Full Report",
                        "onClick": {"openLink": {"url": url}},
                        "color": BUTTON_COLOR,
                    }],
                },
            }],
        },
    ]

    try:
        suggestions = get_incident_suggestions(incident_id)
        if suggestions:
            suggestion_sections = build_suggestion_cards(incident_id, suggestions)
            if suggestion_sections:
                sections.extend(suggestion_sections)
    except Exception:
        pass

    cards_v2 = [{
        "cardId": f"analysis_complete_{incident_id}",
        "card": {
            "header": {
                "title": "Analysis Complete",
            },
            "sections": sections,
        },
    }]

    fallback_text = f"Analysis Complete: {title}"
    return cards_v2, fallback_text


def _build_reroute_card(incident_id: str, alert_title: str, routed_to: str, url: str) -> tuple:
    """Build the "Incident Re-routed" card that replaces the original notification
    for teams that were initially notified but turned out to not be relevant."""
    cards_v2 = [{
        "cardId": f"rerouted_{incident_id}",
        "card": {
            "header": {
                "title": "Incident Re-routed",
                "subtitle": alert_title,
            },
            "sections": [{
                "widgets": [
                    {"textParagraph": {
                        "text": (
                            "This incident was initially routed to your team but "
                            "the investigation determined it belongs to "
                            f"<b>{routed_to}</b>. No action needed."
                        ),
                    }},
                    {
                        "buttonList": {
                            "buttons": [{
                                "text": "View Report",
                                "onClick": {"openLink": {"url": url}},
                                "color": BUTTON_COLOR,
                            }],
                        },
                    },
                ],
            }],
        },
    }]

    fallback_text = f"Incident Re-routed: {alert_title}"
    return cards_v2, fallback_text


# ── Sending helpers ─────────────────────────────────────────────────────


def _send_to_space(client, space_name: str, text: str, cards_v2: List) -> Optional[Dict]:
    """Send a card message to a space. Falls back to plain text if the card payload is rejected (e.g. malformed or too large)."""
    try:
        return client.send_message(space_name=space_name, text=text, cards_v2=cards_v2)
    except Exception:
        try:
            return client.send_message(space_name=space_name, text=text)
        except Exception:
            return None


def _update_or_send(client, space_name: str, message_name: Optional[str], text: str, cards_v2: List) -> bool:
    """Update an existing message if we have its reference from a previous notification (e.g. the "started" card). 
    If we don't have a reference or the update fails, send a new message instead. This keeps one card per
    incident per space instead of cluttering the thread."""
    if message_name:
        try:
            if client.update_message(message_name=message_name, text=text, cards_v2=cards_v2):
                return True
        except Exception:
            pass
    return _send_to_space(client, space_name, text, cards_v2) is not None


# ── DB helpers ──────────────────────────────────────────────────────────


def _get_org_id_for_user(user_id: str) -> Optional[str]:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _get_team_space_mappings(org_id: str) -> List[Dict[str, Any]]:
    """Fetch user-defined team-to-space overrides from the DB."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT team_name, space_name, space_display_name, description "
                    "FROM gchat_team_space_mappings WHERE org_id = %s ORDER BY team_name",
                    (org_id,),
                )
                return [
                    {
                        "team_name": r[0],
                        "space_name": r[1],
                        "space_display_name": r[2],
                        "description": r[3],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        logger.error("[GChatNotification] Error fetching team mappings: %s", e)
        return []


def _get_routing_instructions(org_id: str) -> str:
    """Fetch the free-form routing instructions the user configured in Settings."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT routing_instructions FROM gchat_routing_config WHERE org_id = %s",
                    (org_id,),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else ""
    except Exception:
        return ""


def _store_notified_spaces(incident_id: str, notified: List[Dict[str, Any]]) -> None:
    """Save which spaces we notified (and their message refs) so the completion
    phase can update those messages or replace them with re-route notices.

    Stores under the "google_chat" key in the channel-agnostic notification_refs column."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # Load existing refs from other channels, then merge.
                cur.execute("SELECT notification_refs FROM incidents WHERE id = %s", (incident_id,))
                row = cur.fetchone()
                refs = row[0] if row and row[0] and isinstance(row[0], dict) else {}
                refs["google_chat"] = notified

                cur.execute(
                    "UPDATE incidents SET notification_refs = %s WHERE id = %s",
                    (json.dumps(refs), incident_id),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[GChatNotification] Failed to store notified spaces: %s", e)


def _store_routing_decision(
    incident_id: str,
    phase: str,
    team_names: List[str],
    reason: str,
    dropped: Optional[List[str]] = None,
    added: Optional[List[str]] = None,
) -> None:
    """Persist the routing decision for a given phase ("initial" or "final").

    notified_teams stores the full routing history:
      { "initial": [...], "initial_reason": "...",
        "final": [...],   "final_reason": "...",
        "dropped": [...], "added": [...] }

    routing_reason always holds the latest reason for quick access."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # Load existing data to merge phases.
                cur.execute("SELECT notified_teams FROM incidents WHERE id = %s", (incident_id,))
                row = cur.fetchone()
                existing = row[0] if row and row[0] and isinstance(row[0], dict) else {}

                existing[phase] = team_names
                existing[f"{phase}_reason"] = reason
                if dropped is not None:
                    existing["dropped"] = dropped
                if added is not None:
                    existing["added"] = added

                cur.execute(
                    "UPDATE incidents SET notified_teams = %s, routing_reason = %s WHERE id = %s",
                    (json.dumps(existing), reason, incident_id),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[GChatNotification] Failed to store routing decision: %s", e)


# ── Routing logic ───────────────────────────────────────────────────────


def _get_effective_teams(org_id: str) -> List[Dict[str, Any]]:
    """Build the team list the LLM sees when deciding who to notify.

    1. Auto-detect all Google Chat spaces the bot is a member of.
    2. For each space, check if the user created an override in Settings
       (custom team name / description). If yes, use the override.
       If no, use Google's displayName and description as defaults.
    3. If the bot API is unavailable, fall back to DB overrides only.
    """
    try:
        client = get_chat_app_client()
        bot_spaces = client.list_bot_spaces_summary() if client else []
    except Exception:
        bot_spaces = []

    if not bot_spaces:
        return _get_team_space_mappings(org_id)

    overrides = {m["space_name"]: m for m in _get_team_space_mappings(org_id)}
    teams = []
    for space in bot_spaces:
        space_name = space.get("name", "")
        if space_name in overrides:
            teams.append(overrides[space_name])
        else:
            teams.append({
                "team_name": space.get("displayName", space_name),
                "space_name": space_name,
                "space_display_name": space.get("displayName", ""),
                "description": space.get("description", ""),
            })

    return teams


def _get_team_names_for_spaces(user_id: str, space_names: List[str]) -> Dict[str, str]:
    """Reverse lookup: given a list of space resource names (e.g. "spaces/XXXXX"),
    return a dict mapping each space_name to its human-readable team name."""
    if not space_names:
        return {}
    org_id = _get_org_id_for_user(user_id)
    if not org_id:
        return {}
    try:
        teams = _get_effective_teams(org_id)
        return {
            t["space_name"]: t["team_name"]
            for t in teams
            if t["space_name"] in space_names
        }
    except Exception:
        return {}


def _build_incident_context(incident_data: Dict[str, Any]) -> str:
    """Format incident data as a text block for the routing LLM prompt."""
    parts = [
        f"Title: {incident_data.get('alert_title', 'Unknown')}",
        f"Service: {incident_data.get('service', 'unknown')}",
        f"Severity: {incident_data.get('severity', 'unknown')}",
        f"Source: {incident_data.get('source_type', 'unknown')}",
        f"Environment: {incident_data.get('environment', 'unknown')}",
    ]
    summary = incident_data.get("aurora_summary")
    if summary:
        parts.append(f"Investigation summary:\n{summary[:1500]}")
    return "\n".join(parts)


def _resolve_team_spaces(org_id: str, incident_data: Dict[str, Any]) -> tuple:
    """Use the LLM to decide which team(s) to notify based on the incident
    context, available teams, and the user's routing instructions.

    Returns (space_names, team_names, reason) where reason is the LLM's
    justification for the routing decision.

    Short-circuits the LLM call when there's only one team and no
    routing instructions (the answer is obvious)."""
    teams = _get_effective_teams(org_id)
    if not teams:
        return [], [], ""

    instructions = _get_routing_instructions(org_id)
    if len(teams) == 1 and not instructions:
        return [teams[0]["space_name"]], [teams[0]["team_name"]], "Only one team configured."

    try:
        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from langchain_core.messages import HumanMessage

        teams_text = "\n".join(
            f"- {t['team_name']}: "
            f"{t.get('description') or t.get('space_display_name') or '(no description)'}"
            for t in teams
        )

        prompt = TEAM_ROUTING_PROMPT.format(
            teams=teams_text,
            instructions=instructions or "(none)",
            context=_build_incident_context(incident_data),
        )

        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.0,
            streaming=False,
        )
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id="system",
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="gchat_team_routing",
        )

        raw_response = (response.content or "").strip()
        if not raw_response:
            return [], [], ""

        # Parse the structured response: "TEAMS: ...\nREASON: ..."
        teams_line = ""
        reason = ""
        for line in raw_response.splitlines():
            if line.upper().startswith("TEAMS:"):
                teams_line = line.split(":", 1)[1].strip()
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        if not teams_line or teams_line.upper() == "NONE":
            logger.info("[GChatNotification] LLM routed to no teams. Reason: %s", reason or "(none given)")
            return [], [], reason

        # Map the LLM's team name picks back to space resource names.
        lookup = {t["team_name"].lower(): t for t in teams}
        resolved_spaces = []
        resolved_names = []
        for raw_name in (c.strip() for c in teams_line.split(",")):
            team = lookup.get(raw_name.lower())
            if team:
                resolved_spaces.append(team["space_name"])
                resolved_names.append(team["team_name"])

        if resolved_spaces:
            logger.info(
                "[GChatNotification] Routed to %d team(s): %s | Reason: %s",
                len(resolved_spaces), ", ".join(resolved_names), reason or "(none given)",
            )
        return resolved_spaces, resolved_names, reason

    except Exception as e:
        logger.error("[GChatNotification] LLM routing failed: %s", e, exc_info=True)
        return [], [], ""


def _resolve_notification_spaces(user_id: str, incident_data: Dict[str, Any]) -> tuple:
    """Determine which GChat spaces to notify using instruction-driven team routing.

    Returns (space_names, team_names, reason)."""
    org_id = _get_org_id_for_user(user_id)
    if not org_id:
        return [], [], ""
    return _resolve_team_spaces(org_id, incident_data)


# ── Public API ──────────────────────────────────────────────────────────


def send_google_chat_investigation_started_notification(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Send an "Investigation Started" card to every resolved team space.

    Stores the list of notified spaces and their message references so the
    completion phase can update or re-route them later."""
    try:
        client = get_chat_app_client()
        if not client:
            return False

        spaces, team_names, reason = _resolve_notification_spaces(user_id, incident_data)
        if not spaces:
            return False

        incident_id = incident_data.get("incident_id", "unknown")

        cards_v2, fallback_text = _build_started_card(incident_id, incident_data, team_names)

        # Send to each space and collect message references.
        notified = []
        for space in spaces:
            result = _send_to_space(client, space, fallback_text, cards_v2)
            if result:
                notified.append({
                    "space_name": space,
                    "message_name": result.get("name"),
                })

        if notified:
            _store_notified_spaces(incident_id, notified)
            _store_routing_decision(incident_id, "initial", team_names, reason)

            # Write the first message name to the legacy column so older
            # code paths (e.g. single-space thread replies) still work.
            first_msg = notified[0].get("message_name")
            if first_msg:
                try:
                    with db_pool.get_admin_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE incidents SET google_chat_message_name = %s WHERE id = %s",
                                (first_msg, incident_id),
                            )
                            conn.commit()
                except Exception:
                    pass

            logger.info(
                "[GChatNotification] Sent 'started' for %s to %d space(s)",
                incident_id, len(notified),
            )

        return len(notified) > 0

    except Exception as e:
        logger.error("[GChatNotification] Started notification error: %s", e, exc_info=True)
        return False


def send_google_chat_investigation_completed_notification(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Send an "Analysis Complete" card when the RCA is ready.

    Routing runs again with the full RCA context (which wasn't available at
    the start). Comparing the two routing results produces three groups:

      - kept    = teams that were notified at start AND are still relevant
      - added   = teams the completion routing identified that weren't in the initial set
      - dropped = teams notified at start that are no longer relevant

    Kept + added teams get the "Analysis Complete" card (updating the original
    message in-place when possible). Dropped teams get their original card
    replaced with an "Incident Re-routed" notice."""
    try:
        client = get_chat_app_client()
        if not client:
            return False

        incident_id = incident_data.get("incident_id", "unknown")

        # Load which spaces we notified at the start of the investigation.
        all_refs = incident_data.get("notification_refs") or {}
        prev_entries: List[Dict[str, Any]] = all_refs.get("google_chat") or []
        prev_spaces = {entry["space_name"] for entry in prev_entries}
        prev_message_refs = {entry["space_name"]: entry.get("message_name") for entry in prev_entries}

        # Re-run routing now that we have the full RCA summary.
        current_spaces, current_team_names, reason = _resolve_notification_spaces(user_id, incident_data)
        current_spaces_set = set(current_spaces)

        active_spaces = list((prev_spaces & current_spaces_set) | (current_spaces_set - prev_spaces))
        dropped_spaces = prev_spaces - current_spaces_set

        if not active_spaces and not dropped_spaces:
            return False

        # Build a single team name lookup for all spaces involved.
        all_spaces = list(active_spaces) + list(dropped_spaces)
        team_lookup = _get_team_names_for_spaces(user_id, all_spaces)

        team_names = [team_lookup.get(s, s) for s in active_spaces]
        dropped_names = [team_lookup.get(s, s) for s in dropped_spaces]
        added_spaces = current_spaces_set - prev_spaces
        added_names = [team_lookup.get(s, s) for s in added_spaces]

        _store_routing_decision(
            incident_id, "final", team_names, reason,
            dropped=dropped_names if dropped_names else None,
            added=added_names if added_names else None,
        )

        cards_v2, fallback_text = _build_completed_card(incident_id, incident_data, team_names)

        any_sent = False
        for space in active_spaces:
            original_msg = prev_message_refs.get(space)
            if _update_or_send(client, space, original_msg, fallback_text, cards_v2):
                any_sent = True

        # Replace the original card in dropped spaces with a re-route notice.
        if dropped_spaces:
            url = f"{FRONTEND_URL}/incidents/{incident_id}"
            routed_to = ", ".join(team_names) or "other teams"
            reroute_cards, reroute_text = _build_reroute_card(
                incident_id,
                incident_data.get("alert_title", "Unknown Alert"),
                routed_to,
                url,
            )
            for space in dropped_spaces:
                original_msg = prev_message_refs.get(space)
                if original_msg:
                    try:
                        client.update_message(
                            message_name=original_msg,
                            text=reroute_text,
                            cards_v2=reroute_cards,
                        )
                    except Exception:
                        pass

        return any_sent

    except Exception as e:
        logger.error("[GChatNotification] Completed notification error: %s", e, exc_info=True)
        return False

"""
Description-driven Slack notification routing.

Given an incident, decide which channel(s) an incident notification should go to
by combining two org-scoped sources of truth (the same ones the agent uses):

- the "Slack" memory (``context``/``Slack``) — the team's routing policy/prefs,
- the ``slack_channels`` table — every channel Aurora is aware of, with a
  description of what it's for / which team/service it serves.

A single cheap-model LLM call picks the relevant channel id(s). This mirrors the
agent's forced Slack-memory injection (see ``agent.py``: Slack-sourced sessions
force-inject the same memory) but as a lightweight one-shot call suitable for the
notification hot path — no tool loop.

Always safe: if the LLM is unavailable, returns nothing usable, or the workspace
has no channel table, callers fall back to the org's incidents channel so a
notification is never dropped.
"""
import json
import logging
import re

from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_ROUTING_PROMPT = """You route an incident notification to the most relevant Slack channel(s).

Team's Slack policy (routing preferences):
---
{memory}
---

Channels Aurora can post to (channel_id — name — description):
{channels}

Incident:
- Title: {title}
- Service: {service}
- Severity: {severity}

Pick the channel(s) where this incident's conclusion should be posted, honoring
the policy above. Prefer the single most relevant channel; only pick multiple if
the policy or descriptions clearly call for it. If none is a good fit, return an
empty list (the caller will use the default incidents channel).

Respond with ONLY a JSON array of channel_id strings, e.g. ["C123","C456"]. No prose."""


def _load_candidate_channels(user_id: str) -> list[dict]:
    """Return non-dismissed channels with notify enabled for this org.

    Only ``notify_enabled`` channels are routing candidates — dismissing or
    turning notify off in the UI removes a channel from proactive posting.
    """
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if not set_rls_context(cur, conn, user_id, log_prefix="[SlackRouting]"):
                    return []
                cur.execute(
                    """SELECT DISTINCT ON (channel_id)
                              channel_id, channel_name, metadata_summary
                         FROM slack_channels
                        WHERE provider = 'slack' AND NOT is_dismissed
                          AND notify_enabled = TRUE
                        ORDER BY channel_id, updated_at DESC""",
                )
                return [
                    {"channel_id": r[0], "channel_name": r[1], "description": r[2] or ""}
                    for r in cur.fetchall()
                ]
    except Exception:
        logger.warning("[SlackRouting] Failed to load candidate channels", exc_info=True)
        return []


def _parse_channel_ids(raw: str, valid_ids: set[str]) -> list[str]:
    """Extract a JSON array of channel ids from the LLM output, keep only valid ones."""
    if not raw:
        return []
    # The model may wrap the array in prose/markdown despite instructions.
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        ids = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    # Dedupe while preserving order, and only trust ids we actually know.
    seen = set()
    result = []
    for cid in ids:
        if isinstance(cid, str) and cid in valid_ids and cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


def resolve_notification_channels(user_id: str, incident_data: dict,
                                  default_channel_id: str) -> list[str]:
    """Return the channel id(s) to post an incident notification to.

    Uses the Slack memory + channel table via one cheap-model call. Falls back to
    ``[default_channel_id]`` whenever routing can't confidently decide (no
    candidates, LLM unavailable/blocked, empty/parse-failed result) so a
    notification is never dropped.
    """
    candidates = _load_candidate_channels(user_id)
    # No routing table to reason over — keep the current single-channel behaviour.
    if not candidates:
        return [default_channel_id] if default_channel_id else []

    valid_ids = {c["channel_id"] for c in candidates}

    try:
        # Respect cost gating exactly like the channel-description task.
        from utils.hooks import get_hook
        from utils.auth.stateless_auth import get_org_id_for_user
        org_id = get_org_id_for_user(user_id) if user_id else None
        allowed, message = get_hook("before_llm_call")(org_id, user_id)
        if not allowed:
            logger.info("[SlackRouting] LLM hook blocked routing (%s); using default channel", message)
            return [default_channel_id] if default_channel_id else []

        from services.memory.slack_memory import read_slack_memory
        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from langchain_core.messages import HumanMessage

        memory = read_slack_memory(user_id) or "(no policy recorded)"
        channels_block = "\n".join(
            f"- {c['channel_id']} — #{c['channel_name'] or c['channel_id']} — "
            f"{c['description'] or '(no description)'}"
            for c in candidates
        )
        prompt = _ROUTING_PROMPT.format(
            memory=memory,
            channels=channels_block,
            title=incident_data.get("alert_title") or incident_data.get("title") or "unknown",
            service=incident_data.get("service") or "unknown",
            severity=incident_data.get("severity") or "unknown",
        )

        llm = create_chat_model(ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
                                temperature=0.0, streaming=False)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="slack_notification_routing",
        )
        from chat.backend.agent.utils.message_content import extract_text_from_content
        chosen = _parse_channel_ids(extract_text_from_content(response.content), valid_ids)

        if chosen:
            logger.info("[SlackRouting] routed to %d channel(s) for user %s",
                        len(chosen), sanitize(user_id))
            return chosen
    except Exception:
        logger.warning("[SlackRouting] routing failed; using default channel", exc_info=True)

    # No confident match — default incidents channel keeps us from dropping it.
    return [default_channel_id] if default_channel_id else []

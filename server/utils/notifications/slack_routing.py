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
                                  default_channel_id: str) -> list[dict]:
    """Return the channel(s) to post an incident notification to.

    Each item is ``{"channel_id", "channel_name", "description"}`` — the same
    rows loaded for routing, so callers can compose per-channel messages without
    a second DB read. Uses the Slack memory + channel table via one cheap-model
    call. Falls back to just the default incidents channel (as a bare-id dict)
    whenever routing can't confidently decide (no candidates, LLM
    unavailable/blocked, empty/parse-failed result) so a notification is never
    dropped.
    """
    def _default() -> list[dict]:
        # Default channel isn't necessarily in the table — return a bare-id dict.
        return [{"channel_id": default_channel_id, "channel_name": None, "description": ""}] if default_channel_id else []

    candidates = _load_candidate_channels(user_id)
    # No routing table to reason over — keep the current single-channel behaviour.
    if not candidates:
        return _default()

    by_id = {c["channel_id"]: c for c in candidates}

    try:
        # Respect cost gating exactly like the channel-description task.
        from utils.hooks import get_hook
        from utils.auth.stateless_auth import get_org_id_for_user
        org_id = get_org_id_for_user(user_id) if user_id else None
        allowed, message = get_hook("before_llm_call")(org_id, user_id)
        if not allowed:
            logger.info("[SlackRouting] LLM hook blocked routing (%s); using default channel", message)
            return _default()

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
        chosen = _parse_channel_ids(extract_text_from_content(response.content), set(by_id))

        if chosen:
            # Log the actual channel names so routing decisions are auditable.
            names = ", ".join(f"#{by_id[cid]['channel_name'] or cid}" for cid in chosen)
            logger.info("[SlackRouting] routed incident '%s' to %d channel(s): %s",
                        sanitize(incident_data.get("alert_title") or incident_data.get("title") or "unknown"),
                        len(chosen), names)
            return [by_id[cid] for cid in chosen]
    except Exception:
        logger.warning("[SlackRouting] routing failed; using default channel", exc_info=True)

    # No confident match — default incidents channel keeps us from dropping it.
    return _default()


# Template handed to the model. It gets the incident facts + a base summary, the
# team's Slack policy (org-level tone/format prefs), and the target channel's own
# description (per-channel context), then writes whatever it judges appropriate —
# structured for some teams, a quick human line for others. No fixed format.
_COMPOSE_PROMPT = """You are Aurora, posting an incident update into a specific Slack channel.

Write the message for THIS channel, honoring the team's preferences below.
Some teams want a structured summary; others want a short, human one-liner. Follow
the policy and the channel's purpose — if unsure, keep it concise and useful.

Team's Slack policy (tone / formatting / per-channel preferences):
---
{memory}
---

Target channel:
- #{channel_name} — {channel_description}

Incident facts you may use:
- Title: {title}
- Severity: {severity}
- Service: {service}
- Root cause / summary: {summary}
- Full report link: {url}

Rules:
- Use Slack mrkdwn only (*bold*, _italic_, `code`). No block-kit, no JSON, no HTML.
- Include the report link if it's useful.
- Output ONLY the message text to post — no preamble, no explanation."""


def compose_channel_message(
    user_id: str,
    incident_data: dict,
    *,
    channel_name: str,
    channel_description: str,
    base_summary: str,
    incident_url: str,
    fallback_text: str,
) -> str:
    """Return an LLM-composed Slack message tailored to one channel + org policy.

    Combines the org's Slack memory (tone/format prefs, incl. per-channel notes)
    with the target channel's description so each team gets the shape it wants —
    structured or human — from the same incident. Always safe: returns
    ``fallback_text`` if cost-gated, the LLM is unavailable, or output is empty,
    so notifications never break.
    """
    try:
        from utils.hooks import get_hook
        from utils.auth.stateless_auth import get_org_id_for_user

        org_id = get_org_id_for_user(user_id) if user_id else None
        allowed, message = get_hook("before_llm_call")(org_id, user_id)
        # Cost-gated — fall back to the plain card text rather than skip the post.
        if not allowed:
            logger.info("[SlackCompose] LLM hook blocked compose (%s); using fallback", message)
            return fallback_text

        from services.memory.slack_memory import read_slack_memory
        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from chat.backend.agent.utils.message_content import extract_text_from_content
        from langchain_core.messages import HumanMessage

        prompt = _COMPOSE_PROMPT.format(
            memory=read_slack_memory(user_id) or "(no policy recorded)",
            channel_name=channel_name or "unknown",
            channel_description=channel_description or "(no description)",
            title=incident_data.get("alert_title") or incident_data.get("title") or "unknown",
            service=incident_data.get("service") or "unknown",
            severity=incident_data.get("severity") or "unknown",
            summary=base_summary or "(no summary available)",
            url=incident_url or "(no link)",
        )
        llm = create_chat_model(ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
                                temperature=0.3, streaming=False)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="slack_notification_compose",
        )
        text = (extract_text_from_content(response.content) or "").strip()
        return text or fallback_text
    except Exception:
        logger.warning("[SlackCompose] compose failed; using fallback text", exc_info=True)
        return fallback_text

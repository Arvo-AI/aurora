"""
Celery task to generate LLM-powered descriptions for Slack channels.

Mirrors :mod:`routes.github.github_repo_metadata`. Fetches a channel's
topic/purpose + a small sample of recent messages, then asks an LLM for a
2-3 sentence description of what the channel is for and which team/service it
serves. The result feeds the agent's channel-routing decisions.
"""
import logging

from celery_config import celery_app
from chat.backend.agent.utils.message_content import extract_text_from_content

logger = logging.getLogger(__name__)

METADATA_PROMPT = (
    "Write a 2-3 sentence description of this Slack channel for an AI SRE "
    "teammate. State what the channel is used for, which team or service it "
    "serves, and whether it's an incident/alerting channel (and from which "
    "platform if apparent, e.g. incident.io/PagerDuty/Opsgenie). Infer from the "
    "name, topic, purpose, and recent messages. Output ONLY the description — "
    "no notes, caveats, or markdown headers.\n\n"
    "{context}"
)


def _update_metadata(user_id: str, channel_id: str, summary, status: str,
                     channel_type: str | None = None, platform: str | None = None):
    """Persist a description write, only advancing rows still pending/generating."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            if not set_rls_context(cur, conn, user_id, log_prefix="[SlackChannelMeta]"):
                return
            # Status-only update (generating / error) — don't clobber a summary.
            if summary is None:
                cur.execute(
                    """UPDATE slack_channels
                       SET metadata_status = %s, updated_at = NOW()
                       WHERE user_id = %s AND provider = 'slack' AND channel_id = %s
                         AND metadata_status IN ('pending', 'generating')""",
                    (status, user_id, channel_id),
                )
            else:
                cur.execute(
                    """UPDATE slack_channels
                       SET metadata_summary = %s, metadata_status = %s,
                           channel_type = COALESCE(%s, channel_type),
                           detected_platform = COALESCE(%s, detected_platform),
                           updated_at = NOW()
                       WHERE user_id = %s AND provider = 'slack' AND channel_id = %s
                         AND metadata_status IN ('pending', 'generating')""",
                    (summary, status, channel_type, platform, user_id, channel_id),
                )
            conn.commit()


def _build_context(client, channel_id: str) -> tuple[str, str, dict]:
    """Return (context_text, channel_name, raw_info) for the LLM prompt."""
    info = client.get_channel_info(channel_id) or {}
    name = info.get("name", "")
    topic = (info.get("topic") or {}).get("value", "")
    purpose = (info.get("purpose") or {}).get("value", "")

    parts = [f"Channel name: #{name}"]
    if topic:
        parts.append(f"Topic: {topic}")
    if purpose:
        parts.append(f"Purpose: {purpose}")

    # A few recent messages give the LLM a sense of the channel's actual use.
    # Best-effort: the bot may not be a member, which is fine.
    try:
        result = client._make_request(
            "GET", "conversations.history", {"channel": channel_id, "limit": 15}
        )
        texts = [
            (m.get("text") or "").strip()
            for m in result.get("messages", [])
            if (m.get("text") or "").strip()
        ]
        if texts:
            sample = "\n".join(f"- {t[:200]}" for t in texts[:15])
            parts.append(f"Recent messages:\n{sample}")
    except Exception:
        logger.debug("No history available for channel %s", channel_id)

    return "\n".join(parts), name, info


@celery_app.task(
    name="routes.slack.slack_channel_metadata.generate_channel_metadata",
    bind=True,
    max_retries=2,
)
def generate_channel_metadata(self, user_id: str, channel_id: str):
    """Fetch channel info + recent messages and generate an LLM description."""
    logger.info("Generating metadata for Slack channel %s (user %s)", channel_id, user_id)

    # Respect the LLM-usage hook (cost gating), like the GitHub metadata task.
    from utils.hooks import get_hook
    from utils.auth.stateless_auth import get_org_id_for_user
    _hook_org_id = get_org_id_for_user(user_id) if user_id else None
    hook_allowed, hook_message = get_hook("before_llm_call")(_hook_org_id, user_id)
    if not hook_allowed:
        logger.warning("Hook blocked channel metadata for user %s: %s", user_id, hook_message)
        _update_metadata(user_id, channel_id, None, "limit_reached")
        return

    _update_metadata(user_id, channel_id, None, "generating")

    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        client = get_slack_client_for_user(user_id)
        if not client:
            _update_metadata(user_id, channel_id, None, "error")
            return

        context_text, channel_name, info = _build_context(client, channel_id)

        # Re-classify with the fuller info dict (topic/purpose now available).
        from routes.slack.slack_channels import _classify_channel
        channel_type, platform = _classify_channel(info)

        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from langchain_core.messages import HumanMessage

        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.2,
            streaming=False,
        )
        prompt = METADATA_PROMPT.format(context=context_text)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="slack_channel_metadata",
        )
        summary = extract_text_from_content(response.content).strip() or "No description generated"
        _update_metadata(user_id, channel_id, summary, "ready", channel_type, platform)
        logger.info("Metadata generated for Slack channel %s", channel_id)

    except Exception as e:
        logger.exception("Channel metadata generation failed for %s: %s", channel_id, e)
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _update_metadata(user_id, channel_id, None, "error")

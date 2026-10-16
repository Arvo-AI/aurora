"""LLM channel-description prompt, shared by every chat-platform registry.

Only the template lives here; the Celery task, claim/retry and backfill
orchestration stay in ``routes.slack.slack_channel_metadata`` until a second
provider needs them. Contract: ``render_metadata_prompt("Slack", "name, topic,
purpose", ctx)`` must equal the pre-refactor Slack ``METADATA_PROMPT`` rendering
(pinned by a golden test).
"""

METADATA_PROMPT_TEMPLATE = (
    "Write a 2-3 sentence description of this {platform_display} channel for an AI SRE "
    "teammate. State what the channel is used for, which team or service it "
    "serves, and whether it's an incident/alerting channel (and from which "
    "platform if apparent, e.g. incident.io/PagerDuty/Opsgenie). Infer from the "
    "{fields_hint}, and recent messages. Output ONLY the description — "
    "no notes, caveats, or markdown headers.\n\n"
    "{context}"
)


def render_metadata_prompt(platform_display: str, fields_hint: str, context: str) -> str:
    """``platform_display`` is the noun in "this X channel"; ``fields_hint`` lists
    the provider's descriptive fields ("name, topic, purpose")."""
    return METADATA_PROMPT_TEMPLATE.format(
        platform_display=platform_display, fields_hint=fields_hint, context=context
    )

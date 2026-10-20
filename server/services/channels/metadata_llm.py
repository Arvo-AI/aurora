"""Run the shared channel-description LLM call."""

from __future__ import annotations

from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
from chat.backend.agent.utils.message_content import extract_text_from_content
from langchain_core.messages import HumanMessage
from services.channels.metadata import render_metadata_prompt


def generate_summary(
    user_id: str,
    *,
    platform_display: str,
    fields_hint: str,
    context_text: str,
    request_type: str,
) -> str:
    llm = create_chat_model(
        ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
        temperature=0.2,
        streaming=False,
    )
    prompt = render_metadata_prompt(platform_display, fields_hint, context_text)
    response = tracked_invoke(
        llm,
        [HumanMessage(content=prompt)],
        user_id=user_id,
        model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
        request_type=request_type,
    )
    return extract_text_from_content(response.content).strip() or "No description generated"

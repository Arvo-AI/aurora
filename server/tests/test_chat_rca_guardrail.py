"""Regression coverage for chat-triggered RCA input rail handling."""

from chat.backend.agent.workflow import _get_input_rail_text


def test_rca_input_rail_uses_original_user_question():
    rca_augmented_message = (
        "[RCA INVESTIGATION REQUESTED]\n"
        "The user has explicitly requested a Root Cause Analysis investigation. "
        "You MUST call the trigger_rca tool with their message as the issue_description. "
        "Extract a short title, affected service, and severity from their description.\n\n"
        "there is high cpu memory"
    )

    assert _get_input_rail_text(
        question="there is high cpu memory",
        message_content=rca_augmented_message,
    ) == "there is high cpu memory"


def test_input_rail_falls_back_to_message_content_without_question():
    assert _get_input_rail_text(
        question=None,
        message_content=[{"type": "text", "text": "check high cpu"}],
    ) == "check high cpu"

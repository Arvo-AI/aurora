"""
Trigger Action Tool

LLM-callable tool that dispatches an Aurora Action as a background task.
Called when a user invokes /action from the chat interface.
"""

import json
import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TriggerActionArgs(BaseModel):
    action_id: str = Field(description="The UUID of the action to trigger")


def trigger_action(
    action_id: str,
    user_id: str | None = None,
    **kwargs,
) -> str:
    """
    Trigger an Aurora Action as a background task.

    Args:
        action_id: UUID of the action to dispatch
        user_id: Injected by context wrapper

    Returns:
        JSON string with run_id or error message
    """
    if not user_id:
        return json.dumps({"error": "No user context available."})

    if not action_id:
        return json.dumps({"error": "action_id is required."})

    try:
        from services.actions.executor import dispatch_action
        run_id = dispatch_action(action_id=action_id, user_id=user_id)
        return json.dumps({
            "status": "ok",
            "run_id": run_id,
            "message": "Action dispatched successfully. It will run in the background. Track progress in Settings > Actions.",
        })
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception(f"[TriggerAction] Failed to dispatch action {action_id}: {e}")
        return json.dumps({"error": f"Failed to dispatch action: {e}"})

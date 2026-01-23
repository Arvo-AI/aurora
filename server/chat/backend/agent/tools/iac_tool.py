"""Unified IaC tool wrapper for write/plan/apply Terraform operations."""

from __future__ import annotations

import json
import logging
from typing import Literal, Optional, Any

from langchain_core.tools import StructuredTool

from .iac.iac_write_tool import iac_write
from .iac.iac_commands_tool import (
    iac_plan,
    iac_apply,
    iac_fmt,
    iac_validate,
    iac_destroy,
    iac_refresh,
    iac_outputs,
    iac_state_list,
    iac_state_show,
    iac_state_pull,
)
from chat.backend.agent.access import ModeAccessController
from utils.cloud.cloud_utils import get_mode_from_context

ActionType = Literal[
    "write",
    "fmt",
    "validate",
    "refresh",
    "outputs",
    "state_list",
    "state_show",
    "state_pull",
    "plan",
    "apply",
    "destroy",
]


def run_iac_tool(
    action: ActionType,
    *,
    path: Optional[str] = None,
    content: Optional[str] = None,
    directory: Optional[str] = None,
    vars: Optional[Any] = None,
    auto_approve: Optional[bool] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Dispatch IaC actions through a single entry point.

    Args:
        action: Which operation to execute (write, fmt, validate, refresh, outputs, state_list, state_show, state_pull, plan, apply, destroy).
        path/content: Parameters for write operations.
        directory/vars/auto_approve: Parameters for Terraform CLI operations.
        user_id/session_id: Required context propagated from the agent/frontend.

    Returns:
        JSON string returned by the underlying IaC helper.
    """

    action_lower = action.lower()
    mode = get_mode_from_context()
    is_allowed, denial_message = ModeAccessController.ensure_iac_action_allowed(mode, action_lower)
    if not is_allowed:
        return json.dumps({
            "success": False,
            "error": denial_message,
            "code": "READ_ONLY_MODE",
        })
    
    if action_lower == "write":
        return iac_write(
            path=path or "main.tf",
            content=content or "",
            user_id=user_id,
            session_id=session_id,
        )

    if action_lower == "fmt":
        return iac_fmt(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
        )

    if action_lower == "validate":
        return iac_validate(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
        )

    if action_lower == "refresh":
        return iac_refresh(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
        )

    if action_lower == "outputs":
        return iac_outputs(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
            output_name=vars if isinstance(vars, str) else None,
        )

    if action_lower == "state_list":
        return iac_state_list(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
        )

    if action_lower == "state_show":
        return iac_state_show(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
            state_address=vars if isinstance(vars, str) else None,
        )

    if action_lower == "state_pull":
        return iac_state_pull(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
        )

    if action_lower == "plan":
        return iac_plan(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
            vars=vars,
        )

    if action_lower == "apply":
        return iac_apply(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
            auto_approve=False if auto_approve is None else auto_approve,  # Default to False (require confirmation)
        )

    if action_lower == "destroy":
        return iac_destroy(
            directory=directory or "",
            user_id=user_id,
            session_id=session_id,
            auto_approve=False if auto_approve is None else auto_approve,  # Default to False (require confirmation)
        )

    raise ValueError(f"Unsupported IaC action: {action}")


run_iac_tool.__name__ = "iac_tool"
iac_tool = StructuredTool.from_function(run_iac_tool)

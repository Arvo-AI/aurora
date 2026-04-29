"""MainAgentState — orchestrator-level state extending the chat State."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Optional

from pydantic import ConfigDict

from chat.backend.agent.utils.state import State


def _keep_existing(existing: Any, new: Any) -> Any:
    """Reducer: keep parent/orchestrator value; ignore parallel branch writes."""
    return existing if existing is not None else new


class MainAgentState(State):
    # Reducers below: parallel sub-agent branches write into a shared parent
    # schema namespace. Every key that a branch_state carries — even if all
    # branches carry the same value — triggers an InvalidUpdateError on merge
    # without a reducer. We pin the parent's view by always keeping the existing
    # value, so the main agent isn't overwritten by sub-agent branches.
    agent_id: Annotated[str, _keep_existing] = "main"
    parent_agent_id: Annotated[Optional[str], _keep_existing] = None
    delegate_level: Annotated[int, _keep_existing] = 0
    user_id: Annotated[Optional[str], _keep_existing] = None
    org_id: Annotated[Optional[str], _keep_existing] = None
    session_id: Annotated[Optional[str], _keep_existing] = None
    incident_id: Annotated[Optional[str], _keep_existing] = None
    kb_memory: Annotated[str, _keep_existing] = ""
    active_subagents: list[str] = []
    subagent_results: Annotated[list, operator.add] = []
    plan: Optional[dict] = None
    multi_agent_config: dict = {}
    complexity: Optional[str] = None
    replan_count: int = 0
    next_action: Optional[str] = None
    cited_artifact: Optional[dict] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

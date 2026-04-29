"""SubAgentState + SubAgentResult — the per-branch state contract."""
from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SubAgentState(BaseModel):
    agent_id: str
    parent_agent_id: str
    purpose: str
    suggested_skill_focus: list[str] = Field(default_factory=list)
    incident_summary: str = ""
    kb_memory: str = ""
    user_id: Optional[str] = None
    org_id: Optional[str] = None
    session_id: Optional[str] = None
    incident_id: Optional[str] = None
    delegate_level: int = 1
    tools_used: list[str] = Field(default_factory=list)
    findings_artifact_ref: Optional[str] = None
    status: str = "running"
    error: Optional[str] = None
    model_used: Optional[str] = None
    react_capture: Optional[dict] = None
    # Mirrors MainAgentState.subagent_results so the subgraph's terminal node can
    # write into it and LangGraph propagates the merged list up to the parent.
    subagent_results: Annotated[list, operator.add] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class SubAgentResult(BaseModel):
    agent_id: str
    purpose: str
    status: Literal["succeeded", "failed", "cancelled"]
    findings_artifact_ref: Optional[str] = None
    error: Optional[str] = None

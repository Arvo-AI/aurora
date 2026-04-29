"""Optional confidence-ranking tool the orchestrator may invoke over findings."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_RANK_SYSTEM_PROMPT = (
    "You are a judge ranking sub-agent findings for an incident RCA. "
    "Given a JSON array of findings, return a JSON object with key 'rankings' that "
    "is an array of {agent_id, confidence, rationale}. "
    "confidence is a float in [0.0, 1.0]; rationale is one short sentence. "
    "Preserve the same agent_ids that appear in the input. JSON only."
)


class RankedFinding(BaseModel):
    agent_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class RankedFindings(BaseModel):
    rankings: list[RankedFinding] = Field(default_factory=list)


class RankFindingsInput(BaseModel):
    findings: list[dict[str, Any]] = Field(default_factory=list)
    user_id: Optional[str] = None
    org_id: Optional[str] = None


def _fallback(findings: list[dict], note: str) -> list[dict]:
    out: list[dict] = []
    for item in findings or []:
        enriched = dict(item)
        enriched.setdefault("confidence", 0.5)
        enriched.setdefault("rationale", note)
        out.append(enriched)
    return out


async def rank_findings(
    findings: list[dict],
    user_id: Optional[str] = None,
    org_id: Optional[str] = None,
) -> list[dict]:
    if not findings:
        return []

    try:
        from chat.backend.agent.llm import resolve_role_model
        from chat.backend.agent.providers import create_chat_model

        provider, model_id = resolve_role_model("judge", user_id, org_id)
        if not model_id:
            logger.warning("[orchestrator:rank_findings] no judge model resolved")
            return _fallback(findings, "judge_model_unavailable")

        model_spec = f"{provider}/{model_id}" if provider else model_id
        llm = create_chat_model(model_spec, temperature=0).with_structured_output(RankedFindings)

        compact = [
            {
                "agent_id": f.get("agent_id", f"unknown-{i}"),
                "summary": (f.get("summary") or f.get("findings_artifact_ref") or "")[:2000],
                "status": f.get("status"),
            }
            for i, f in enumerate(findings)
        ]
        prompt = f"{_RANK_SYSTEM_PROMPT}\n\nFindings:\n{json.dumps(compact, default=str)}"
        result = await llm.ainvoke(prompt)
        rankings = {r.agent_id: (r.confidence, r.rationale) for r in result.rankings}
    except Exception as e:
        logger.warning("[orchestrator:rank_findings] judge failed: %s", e)
        return _fallback(findings, "judge_model_unavailable")

    out: list[dict] = []
    for item in findings:
        enriched = dict(item)
        agent_id = enriched.get("agent_id")
        if agent_id in rankings:
            conf, rat = rankings[agent_id]
            enriched["confidence"] = float(conf)
            enriched["rationale"] = rat
        else:
            enriched.setdefault("confidence", 0.5)
            enriched.setdefault("rationale", "judge_no_ranking")
        out.append(enriched)
    return out


rank_findings_tool = StructuredTool.from_function(
    coroutine=rank_findings,
    name="rank_findings",
    description=(
        "Rank a list of sub-agent findings dicts by judge-model confidence. "
        "Returns the same list with `confidence: float` and `rationale: str` per item."
    ),
    args_schema=RankFindingsInput,
)

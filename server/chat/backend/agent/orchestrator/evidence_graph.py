"""Bounded, deterministic evidence-to-hypothesis graph for RCA findings.

Graph IDs are local to a single findings artifact; no network or LLM calls.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_EVIDENCE = 64
MAX_HYPOTHESES = 32
MAX_RELATIONSHIPS = 128


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    observation: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=128)
    reference: str = Field(min_length=1, max_length=1024)
    observed_at: datetime | None = None
    verification: Literal["observed", "reported", "unverified"] = "unverified"

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("observed_at must include a timezone")
        return value


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    statement: str = Field(min_length=1, max_length=2000)
    status: Literal["open", "supported", "contradicted", "inconclusive"] = "open"


class EvidenceRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    hypothesis_id: str
    relation: Literal["supports", "contradicts", "context"]


class EvidenceGraph(BaseModel):
    """Bipartite graph. O(V+E) reference checks using ID sets."""
    model_config = ConfigDict(extra="forbid")
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=MAX_HYPOTHESES)
    relationships: list[EvidenceRelation] = Field(default_factory=list, max_length=MAX_RELATIONSHIPS)

    @model_validator(mode="after")
    def validate_graph(self) -> "EvidenceGraph":
        evidence_ids = {item.id for item in self.evidence}
        hypothesis_ids = {item.id for item in self.hypotheses}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("duplicate evidence ID")
        if len(hypothesis_ids) != len(self.hypotheses):
            raise ValueError("duplicate hypothesis ID")
        seen = set()
        for edge in self.relationships:
            if edge.evidence_id not in evidence_ids:
                raise ValueError(f"unknown evidence ID: {edge.evidence_id}")
            if edge.hypothesis_id not in hypothesis_ids:
                raise ValueError(f"unknown hypothesis ID: {edge.hypothesis_id}")
            key = (edge.evidence_id, edge.hypothesis_id, edge.relation)
            if key in seen:
                raise ValueError("duplicate evidence relationship")
            seen.add(key)
        return self

    def synthesis_context(self, max_chars: int = 4000) -> str:
        """Render bounded, deterministic context; do not calculate fake probabilities."""
        if not self.evidence and not self.hypotheses:
            return ""
        by_hypothesis: dict[str, list[EvidenceRelation]] = {}
        for edge in self.relationships:
            by_hypothesis.setdefault(edge.hypothesis_id, []).append(edge)
        evidence_by_id = {item.id: item for item in self.evidence}
        lines = ["STRUCTURED EVIDENCE GRAPH (agent-provided; verify source references):"]
        for hypothesis in self.hypotheses:
            lines.append(f"Hypothesis {hypothesis.id} [{hypothesis.status}]: {hypothesis.statement}")
            for edge in by_hypothesis.get(hypothesis.id, []):
                ev = evidence_by_id[edge.evidence_id]
                lines.append(
                    f"  {edge.relation}: {ev.id} [{ev.verification}] "
                    f"{ev.observation} (source={ev.source}, ref={ev.reference})"
                )
                result = "\n".join(lines)
                if len(result) <= max_chars:
                    return result

                marker = "\n[graph context truncated]"
                budget = max_chars - len(marker)

                if budget <= 0:
                    return marker[:max_chars]

                kept: list[str] = []
                used = 0

                for line in lines:
                    extra = len(line) + (1 if kept else 0)
                    if used + extra > budget:
                        break
                    kept.append(line)
                    used += extra

                return "\n".join(kept) + marker

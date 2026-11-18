"""Public sample data and the evidence-backed investigation contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CaseId = Literal["case-a", "case-b", "case-c"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    id: str
    title: str
    kind: Literal["alert", "logs", "metrics", "change", "trace"]
    content: str


class SampleCase(StrictModel):
    id: CaseId
    label: str
    alert: str
    provenance: str
    evidence: list[Evidence] = Field(min_length=3)

    @model_validator(mode="after")
    def unique_evidence(self):
        ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("Evidence IDs must be unique")
        return self


class InvestigateRequest(StrictModel):
    case_id: CaseId


QuestionId = Literal["alternatives", "evidence", "falsify"]
QUESTIONS = {
    "alternatives": "Could something else explain this error?",
    "evidence": "Which evidence makes the difference?",
    "falsify": "What would prove your diagnosis wrong?",
}


class ChallengeRequest(StrictModel):
    run_token: str = Field(min_length=40, max_length=60000)
    question_id: QuestionId


class Finding(StrictModel):
    explanation: str = Field(min_length=10, max_length=1800)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class Diagnosis(StrictModel):
    root_cause: Finding
    observations: list[Finding] = Field(min_length=2, max_length=5)
    alternative: Finding
    uncertainty: str = Field(min_length=10, max_length=1200)
    next_check: str = Field(min_length=10, max_length=1200)

    def cited_ids(self) -> set[str]:
        findings = [self.root_cause, *self.observations, self.alternative]
        return {ref for finding in findings for ref in finding.evidence_ids}


class ChallengeFinding(Finding):
    explanation: str = Field(min_length=10, max_length=400)


class ChallengeAnswer(StrictModel):
    answer: str = Field(min_length=10, max_length=600)
    supporting_evidence: list[ChallengeFinding] = Field(min_length=1, max_length=2)
    uncertainty: str = Field(min_length=10, max_length=400)
    next_check: str = Field(min_length=10, max_length=400)

    def cited_ids(self) -> set[str]:
        return {ref for finding in self.supporting_evidence for ref in finding.evidence_ids}

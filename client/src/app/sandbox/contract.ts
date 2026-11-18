/** Validate HTTP responses before any field reaches the incident cards. */
import type { QuestionId } from "../../lib/sandbox-questions";
export type Evidence = { id: string; title: string; kind: string; content: string };
export type SampleCase = { id: string; label: string; alert: string; provenance: string; evidence: Evidence[] };
export type Finding = { explanation: string; evidence_ids: string[] };
export type Investigation = {
  case_id: string;
  run_id: string;
  run_token?: string;
  model: string;
  elapsed_ms: number;
  diagnosis: {
    root_cause: Finding;
    observations: Finding[];
    alternative: Finding;
    uncertainty: string;
    next_check: string;
  };
  retrieved_evidence_ids: string[];
  tool_calls: { tool: string; evidence_id: string | null }[];
};
export type ChallengeResult = {
  case_id: string;
  run_id: string;
  investigation_run_id: string;
  question_id: QuestionId;
  model: string;
  elapsed_ms: number;
  response: { answer: string; supporting_evidence: Finding[]; uncertainty: string; next_check: string };
  retrieved_evidence_ids: string[];
  tool_calls: { tool: string; evidence_id: string | null }[];
};

const caseIds = ["case-a", "case-b", "case-c"];
const kinds = ["alert", "logs", "metrics", "change", "trace"];
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const uniqueIds = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(text) && new Set(value).size === value.length;

export class SandboxResponseError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function readSandboxResponse(response: Response): Promise<unknown> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error(response.status === 429
      ? "The sandbox is busy or has reached its request allowance. Please try again later."
      : "The sandbox returned an unreadable response. Please retry.");
  }
  if (!response.ok) {
    throw new SandboxResponseError(record(data) && text(data.error) && data.error.length <= 500
      ? data.error : "This request could not finish. Please retry.", response.status);
  }
  return data;
}

export function parseCases(data: unknown): SampleCase[] {
  const valid = record(data) && Array.isArray(data.cases) && data.cases.length === 3
    && data.cases.every(sample => record(sample)
      && text(sample.id) && caseIds.includes(sample.id)
      && [sample.label, sample.alert, sample.provenance].every(text)
      && Array.isArray(sample.evidence) && sample.evidence.length >= 3
      && sample.evidence.every(item => record(item)
        && [item.id, item.title, item.kind, item.content].every(text)
        && kinds.includes(item.kind as string)
        && (item.id as string).startsWith(`${(sample.id as string).slice(-1)}-`))
      && new Set(sample.evidence.map(item => item.id)).size === sample.evidence.length)
    && new Set(data.cases.map(sample => sample.id)).size === 3;
  if (!valid) throw new Error("Sample evidence is incomplete or invalid. Please reload it.");
  return (data as { cases: SampleCase[] }).cases;
}

export function parseInvestigation(data: unknown, sample: SampleCase): Investigation {
  const evidenceIds = new Set(sample.evidence.map(item => item.id));
  const retrievedIds = new Set(record(data) && uniqueIds(data.retrieved_evidence_ids) ? data.retrieved_evidence_ids : []);
  const finding = (value: unknown) => record(value) && text(value.explanation)
    && Array.isArray(value.evidence_ids) && value.evidence_ids.length > 0 && value.evidence_ids.every(text)
    && value.evidence_ids.every(id => evidenceIds.has(id) && retrievedIds.has(id));
  const valid = record(data) && data.status === "complete" && data.case_id === sample.id
    && text(data.run_id) && text(data.model)
    && (data.run_token === undefined || (text(data.run_token) && data.run_token.length <= 60000))
    && typeof data.elapsed_ms === "number" && Number.isFinite(data.elapsed_ms) && data.elapsed_ms >= 0
    && uniqueIds(data.retrieved_evidence_ids) && data.retrieved_evidence_ids.every(id => evidenceIds.has(id))
    && record(data.diagnosis) && finding(data.diagnosis.root_cause) && finding(data.diagnosis.alternative)
    && Array.isArray(data.diagnosis.observations) && data.diagnosis.observations.length >= 2
    && data.diagnosis.observations.every(finding)
    && [data.diagnosis.uncertainty, data.diagnosis.next_check].every(text)
    && Array.isArray(data.tool_calls) && data.tool_calls.every(call => record(call)
      && ["list_evidence", "read_evidence", "submit_diagnosis"].includes(call.tool as string)
      && (call.tool === "read_evidence" ? text(call.evidence_id) && evidenceIds.has(call.evidence_id) : call.evidence_id === null));
  if (!valid) throw new Error("This incident returned an incomplete or mismatched result. Please retry it.");
  return data as Investigation;
}

export function parseChallenge(data: unknown, sample: SampleCase, investigation: Investigation, questionId: QuestionId): ChallengeResult {
  const evidenceIds = new Set(sample.evidence.map(item => item.id));
  const retrievedIds = new Set(record(data) && uniqueIds(data.retrieved_evidence_ids) ? data.retrieved_evidence_ids : []);
  const finding = (value: unknown) => record(value) && text(value.explanation)
    && Array.isArray(value.evidence_ids) && value.evidence_ids.length > 0 && value.evidence_ids.every(text)
    && value.evidence_ids.every(id => evidenceIds.has(id) && retrievedIds.has(id));
  const valid = record(data) && data.status === "complete" && data.case_id === sample.id
    && data.investigation_run_id === investigation.run_id && data.question_id === questionId
    && text(data.run_id) && text(data.model)
    && typeof data.elapsed_ms === "number" && Number.isFinite(data.elapsed_ms) && data.elapsed_ms >= 0
    && uniqueIds(data.retrieved_evidence_ids) && data.retrieved_evidence_ids.every(id => evidenceIds.has(id))
    && record(data.response) && [data.response.answer, data.response.uncertainty, data.response.next_check].every(text)
    && Array.isArray(data.response.supporting_evidence) && data.response.supporting_evidence.length >= 1
    && data.response.supporting_evidence.every(finding)
    && Array.isArray(data.tool_calls) && data.tool_calls.every(call => record(call)
      && ["list_evidence", "read_evidence", "submit_challenge"].includes(call.tool as string)
      && (call.tool === "read_evidence" ? text(call.evidence_id) && evidenceIds.has(call.evidence_id) : call.evidence_id === null));
  if (!valid) throw new Error("This challenge returned an incomplete or mismatched response. Please retry it.");
  return data as ChallengeResult;
}

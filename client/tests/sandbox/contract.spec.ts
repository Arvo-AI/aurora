import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseCases, parseChallenge, parseInvestigation, readSandboxResponse } from '../../src/app/sandbox/contract';

const cases = ['a', 'b', 'c'].map(letter => JSON.parse(readFileSync(resolve(__dirname, `../../../server/services/sandbox/data/case-${letter}.json`), 'utf8')));
const sample = cases[0];
const finding = { explanation: 'A supported test finding.', evidence_ids: ['a-02'] };
const result = () => ({
  status: 'complete', case_id: 'case-a', run_id: 'test-run', model: 'test-model', elapsed_ms: 120,
  diagnosis: { root_cause: finding, observations: [finding, finding], alternative: finding, uncertainty: 'Further checking is needed.', next_check: 'Inspect another signal.' },
  retrieved_evidence_ids: ['a-02'], tool_calls: [{ tool: 'read_evidence', evidence_id: 'a-02' }],
});

test('actual evidence bundles and a supported response can render', () => {
  expect(parseCases({ cases })).toHaveLength(3);
  expect(parseInvestigation(result(), sample).case_id).toBe('case-a');
});

for (const [name, response] of Object.entries({
  empty: {}, null: null, missingCase: { cases: cases.slice(1) }, duplicateCase: { cases: [sample, sample, cases[2]] },
  missingEvidence: { cases: [{ ...sample, evidence: null }, ...cases.slice(1)] },
  foreignEvidence: { cases: [{ ...sample, evidence: cases[1].evidence }, ...cases.slice(1)] },
})) {
  test(`rejects ${name} sample response without rendering broken cards`, () => {
    expect(() => parseCases(response)).toThrow('Sample evidence is incomplete or invalid');
  });
}

test('rejects mismatched, malformed and ungrounded results', () => {
  const invalid = [
    null, {}, { ...result(), case_id: 'case-b' }, { ...result(), diagnosis: {} },
    { ...result(), elapsed_ms: -1 }, { ...result(), tool_calls: null },
    { ...result(), retrieved_evidence_ids: [] },
    { ...result(), retrieved_evidence_ids: ['a-02', 'b-01'] },
    { ...result(), tool_calls: [{ tool: 'read_evidence', evidence_id: 'b-01' }] },
  ];
  for (const response of invalid) expect(() => parseInvestigation(response, sample)).toThrow('incomplete or mismatched');
});

test('a malformed incident does not prevent parsing other independent results', () => {
  expect(() => parseInvestigation({}, sample)).toThrow();
  expect(parseInvestigation(result(), sample).run_id).toBe('test-run');
});

test('HTML or interrupted responses produce a retry message, never raw HTML', async () => {
  await expect(readSandboxResponse(new Response('<h1>upstream unavailable</h1>', { status: 502 }))).rejects.toThrow('unreadable response');
  await expect(readSandboxResponse(new Response('<h1>rate limited</h1>', { status: 429 }))).rejects.toThrow('request allowance');
});

test('preserves safe actionable quota errors', async () => {
  const response = Response.json({ error: 'Check API credits before retrying.' }, { status: 503 });
  await expect(readSandboxResponse(response)).rejects.toThrow('Check API credits');
});

const challengeResult = () => ({
  status: 'complete', case_id: 'case-a', run_id: 'challenge-1', investigation_run_id: 'test-run', question_id: 'evidence',
  model: 'test-model', elapsed_ms: 80, retrieved_evidence_ids: ['a-02'],
  response: { answer: 'The observations distinguish these causes.', supporting_evidence: [finding], uncertainty: 'Only this sample is available.', next_check: 'Inspect an independent signal.' },
  tool_calls: [{ tool: 'read_evidence', evidence_id: 'a-02' }, { tool: 'submit_challenge', evidence_id: null }],
});

test('accepts an evidence-backed response to the selected original investigation', () => {
  expect(parseChallenge(challengeResult(), sample, result(), 'evidence').investigation_run_id).toBe('test-run');
});

test('rejects responses for another run, case or question and malformed responses', () => {
  for (const data of [
    {}, null, { ...challengeResult(), investigation_run_id: 'stale-run' },
    { ...challengeResult(), case_id: 'case-b' }, { ...challengeResult(), question_id: 'falsify' },
    { ...challengeResult(), response: {} }, { ...challengeResult(), retrieved_evidence_ids: [] },
    { ...challengeResult(), tool_calls: [{ tool: 'read_evidence', evidence_id: 'b-02' }] },
  ]) expect(() => parseChallenge(data, sample, result(), 'evidence')).toThrow('incomplete or mismatched');
});

test('repeated valid citations remain renderable and can be deduplicated by the view', () => {
  const data = result();
  data.diagnosis.root_cause = { ...finding, evidence_ids: ['a-02', 'a-02'] };
  expect(parseInvestigation(data, sample).diagnosis.root_cause.evidence_ids).toEqual(['a-02', 'a-02']);
});

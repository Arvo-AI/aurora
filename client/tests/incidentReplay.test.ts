import test from 'node:test';
import assert from 'node:assert/strict';
import { prepareEvidence, redactSecrets, validateReport } from '../src/lib/incidentReplay.ts';

test('rejects missing and oversized evidence', () => {
  assert.throws(() => prepareEvidence('hello', ''), /Both fields/);
  assert.throws(() => prepareEvidence('hello', 'x'.repeat(12001)), /Input too large/);
  assert.throws(() => prepareEvidence('hello', 'line\n'.repeat(221)), /Too many log lines/);
});

test('redacts common credential patterns before model request', () => {
  const result = redactSecrets('password=mysecret\nAuthorization: Bearer abcdefgh');
  assert.doesNotMatch(result, /mysecret|abcdefgh/);
  assert.match(result, /\[REDACTED\]/);
});

test('line numbering remains stable and blank lines are removed', () => {
  assert.deepEqual(prepareEvidence('Alert', 'first\n\n second').evidence, [
    { id: 1, text: 'first' }, { id: 2, text: 'second' },
  ]);
});

test('model cannot cite nonexistent evidence or present unsupported observations', () => {
  const result = validateReport({
    summary: 'Possibly a database error',
    observations: [
      { observation: 'Database refused', evidence_ids: [1, 9000] },
      { observation: 'Cloud is offline', evidence_ids: [300] },
    ],
    hypotheses: [{ observation: 'Deployment broke database', evidence_ids: [999] }],
    next_checks: ['Inspect database availability'],
  }, [{ id: 1, text: 'connection refused' }]);
  assert.deepEqual(result.observations, [{ observation: 'Database refused', evidence_ids: [1] }]);
  assert.equal(result.hypotheses[0].evidence_ids.length, 0);
  assert.equal(result.evidence_only, true);
});

test('handles incomplete model response without fabricating verified RCA', () => {
  const result = validateReport({}, [{id: 1, text: 'HTTP 503'}]);
  assert.equal(result.observations.length, 0);
  assert.match(result.limitations, /No live production/);
});

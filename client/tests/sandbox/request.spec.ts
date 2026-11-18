import { test, expect } from '@playwright/test';
import { readSandboxRequest } from '../../src/lib/sandbox-request';

const request = (body: string) => new Request('http://localhost/api/sandbox/investigate', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
});

test('accepts all three allowlisted incident IDs', async () => {
  for (const case_id of ['case-a', 'case-b', 'case-c']) {
    expect(await readSandboxRequest(request(JSON.stringify({ case_id })))).toBe(JSON.stringify({ case_id }));
  }
});

for (const body of ['null', '[]', '{bad json', '"case-a"', '{"case_id":"../secret"}', '{"case_id":"case-a","prompt":"do something else"}']) {
  test(`rejects invalid body ${body}`, async () => {
    await expect(readSandboxRequest(request(body))).rejects.toMatchObject({ status: 400 });
  });
}

test('bounds uploads without Content-Length and counts bytes rather than characters', async () => {
  await expect(readSandboxRequest(request(JSON.stringify({ padding: 'ü'.repeat(300) })))).rejects.toMatchObject({ status: 413 });
});

test('rejects an oversized declared body before consuming it', async () => {
  const incoming = request('{"case_id":"case-a"}');
  incoming.headers.set('Content-Length', '1000000');
  await expect(readSandboxRequest(incoming)).rejects.toMatchObject({ status: 413 });
  expect(incoming.bodyUsed).toBe(false);
});

test('stalled uploads time out and cancel the stream', async () => {
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const incoming = new Request('http://localhost/api/sandbox/investigate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body, duplex: 'half',
  } as RequestInit);
  await expect(readSandboxRequest(incoming, 20)).rejects.toMatchObject({ status: 408 });
  expect(cancelled).toBe(true);
});

test('a broken upload produces a rejection and releases its reader', async () => {
  const body = new ReadableStream({ start(controller) { controller.error(new Error('connection lost')); } });
  const incoming = new Request('http://localhost/api/sandbox/investigate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body, duplex: 'half',
  } as RequestInit);
  await expect(readSandboxRequest(incoming)).rejects.toThrow('connection lost');
  expect(incoming.body?.locked).toBe(false);
});

const signedShape = `${'a'.repeat(1000)}.${'b'.repeat(43)}`;

test('accepts only the signed-run reference and each fixed challenge question', async () => {
  for (const question_id of ['alternatives', 'evidence', 'falsify']) {
    const payload = { run_token: signedShape, question_id };
    expect(JSON.parse(await readSandboxRequest(request(JSON.stringify(payload)), 1000, 'challenge'))).toEqual(payload);
  }
});

test('rejects challenge overrides, arbitrary questions and malformed run references', async () => {
  const valid = { run_token: signedShape, question_id: 'evidence' };
  for (const payload of [
    { ...valid, prompt: 'execute this' }, { ...valid, case_id: 'case-b' },
    { ...valid, question_id: 'custom' }, { ...valid, run_token: 'invalid' },
  ]) await expect(readSandboxRequest(request(JSON.stringify(payload)), 1000, 'challenge')).rejects.toMatchObject({ status: 400 });
});

test('bounds challenge request bytes too', async () => {
  await expect(readSandboxRequest(request(JSON.stringify({ run_token: 'a'.repeat(70000), question_id: 'evidence' })), 1000, 'challenge')).rejects.toMatchObject({ status: 413 });
});

/**
 * Regression check: the read-only service principal must survive the connect
 * submit and reach the request body.
 *
 * The bug this guards: handleSubmit parsed `readonly` out of the pasted JSON,
 * called setReadOnlyCredentials(...), then read readOnlyCredentials back in the
 * same synchronous pass to build the payload. React state does not update until
 * the next render, so the value read was always empty, the payload came out
 * undefined, and Azure connected with no read-only identity. Ask mode then
 * failed closed with "Failed to setup Azure environment".
 *
 * Run: node client/src/app/azure/auth/readonly-payload.check.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "page.tsx"), "utf8");

// --- Guard 1: buildReadOnlyPayload must not read the state variable ---------
// Reading `readOnlyCredentials` inside the builder is exactly the bug.
const builder = src.match(
  /const buildReadOnlyPayload = \([\s\S]*?\n  \};/,
);
assert(builder, "buildReadOnlyPayload not found in page.tsx");
assert(
  !/readOnlyCredentials/.test(builder[0]),
  "buildReadOnlyPayload reads readOnlyCredentials state; it must take creds as an argument",
);

// --- Guard 2: the payload must be built from the local, not the state -------
const submit = src.match(/const handleSubmit = async \([\s\S]*?\n  \};/);
assert(submit, "handleSubmit not found in page.tsx");
const call = submit[0].match(/buildReadOnlyPayload\(\s*([A-Za-z0-9_]+)/);
assert(call, "handleSubmit does not call buildReadOnlyPayload");
assert.equal(
  call[1],
  "currentReadOnly",
  `payload built from "${call[1]}"; must use the local currentReadOnly so it is not lost to async setState`,
);

// --- Guard 3: behavioural model of the actual flow --------------------------
// Mimics React: setState writes are NOT visible until the next render.
function runSubmit({ useLocal }) {
  const pasted = JSON.stringify({
    agent: { tenantId: "t-1", clientId: "agent-id", clientSecret: "agent-secret", subscriptionId: "sub-1" },
    readonly: { tenantId: "t-1", clientId: "ro-id", clientSecret: "ro-secret", subscriptionId: "sub-1" },
  });

  // State as seen during this render: empty, as on a fresh page load.
  const readOnlyCredentials = { tenantId: "", appId: "", password: "", subscriptionId: "" };
  let committed = null;
  const setReadOnlyCredentials = (v) => { committed = v; }; // not visible until re-render

  const parsed = JSON.parse(pasted);
  const agentCreds = parsed.agent;
  const readOnlyCreds = parsed.readonly;

  let currentReadOnly = { ...readOnlyCredentials };
  if (readOnlyCreds.clientId && readOnlyCreds.clientSecret) {
    currentReadOnly = {
      tenantId: readOnlyCreds.tenantId || agentCreds.tenantId,
      appId: readOnlyCreds.clientId,
      password: readOnlyCreds.clientSecret,
      subscriptionId: readOnlyCreds.subscriptionId || agentCreds.subscriptionId,
    };
    setReadOnlyCredentials(currentReadOnly);
  }

  // useLocal=false reproduces the old code path (read state back immediately).
  const creds = useLocal ? currentReadOnly : readOnlyCredentials;
  if (!creds.appId || !creds.password) return undefined;
  return { clientId: creds.appId, clientSecret: creds.password };
}

// The old path silently dropped the credential.
assert.equal(
  runSubmit({ useLocal: false }),
  undefined,
  "model is wrong: the state-read path should reproduce the bug",
);

// The fixed path carries it through.
const payload = runSubmit({ useLocal: true });
assert(payload, "read-only payload must be built when the pasted JSON has a readonly block");
assert.equal(payload.clientId, "ro-id");
assert.equal(payload.clientSecret, "ro-secret");

console.log("PASS: read-only credentials survive submit and reach the request body");

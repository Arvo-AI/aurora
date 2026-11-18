# Three independent sample investigations

This Option A contribution lets a visitor evaluate an evidence-backed investigation
without creating an account, connecting infrastructure or waiting for an incident.
The sandbox contains three independent investigations and evidence-backed
follow-up challenges to each completed diagnosis.

## What runs

- `/sandbox` is a public Next.js page with three independently completing cards.
- Each incident starts with the same connection-acquisition timeout.
- A live tool-using model retrieves records and submits a structured diagnosis.
- Evidence is visible before and after investigation. Citations open the records.
- A failed case can be retried without rerunning the other two.
- After a diagnosis, select one of three fixed questions: competing causes,
  distinguishing evidence, or a falsifiable check. Answers are generated live,
  with cited observations, uncertainty and a proposed next check.
- Completed answers are retained in the current comparison. Selecting the same
  question again reuses its answer; failures can be retried independently.

All evidence is **authored synthetic data**, not captured customer telemetry or a
live simulated cluster. Its provenance is disclosed in the UI and each bundle.
The exercise demonstrates behavior on these cases, not production accuracy.

The investigator uses Aurora's shared `build_agent_graph` runtime and model
provider factory with an evidence-specific prompt and tools. It does not invoke
the full production RCA workflow, connectors, tenant memory or customer storage.
The shared graph factory is also used by the production agent without changing
its tool or middleware configuration.

## Run locally

Python 3.12+ and Node.js 20+ are required. No Docker, customer credentials,
PostgreSQL, Vault or Redis are needed for this standalone loopback runner.

1. Install the repository's server dependencies in your virtual environment:
   `python -m pip install -r server/requirements.txt`.
2. In the root `.env`, set an existing Aurora model configuration, for example:

   ```dotenv
   MAIN_MODEL=openai/gpt-5.2
   LLM_PROVIDER_MODE=openai
   OPENAI_API_KEY=<your own API key, stored locally only>
   INTERNAL_API_SECRET=<generate a random secret>
   ```

3. In `client/.env.local`, set:

   ```dotenv
   AURORA_ENV=dev
   BACKEND_URL=http://127.0.0.1:5080
   INTERNAL_API_SECRET=<the same internal secret>
   AUTH_SECRET=<generate another random secret>
   ```

4. Start `python server/sandbox_app.py` in one terminal.
5. In another terminal, run `cd client`, `npm ci`, then `npm run dev`.
6. Open `http://localhost:3000/sandbox` and choose **Investigate all three**.

Use your own provider credentials. An API key alone does not provide API credits
or model access. Quota, authentication and timeout errors are shown honestly;
there is no canned success fallback. Keep `.env` files out of version control.
Never ask visitors to enter API keys into a publicly hosted sandbox.

## Isolation and limits

Each call constructs a fresh graph and evidence context. OpenAI-compatible runs also own and close a separate async HTTP client so separate Flask request loops cannot reuse a stale connection pool. The only tools are
`list_evidence`, `read_evidence` and `submit_diagnosis`. Their closures can read
only the selected allowlisted bundle. Client requests accept only a case ID;
they cannot supply prompts, model names, user IDs, paths or credentials.
The answer rubric lives under tests and is not loaded by the runtime.

Challenges accept only a signed run reference and a fixed question ID. The
reference authenticates the original run ID, diagnosis, case and evidence hash;
client-edited diagnoses and case overrides are not accepted. It contains only
public sample findings, never the signing key or provider credentials. It expires
after one hour; evidence changes or signing-key rotation require a new comparison.
Signing uses the existing internal secret with a sandbox-specific domain prefix,
so it works across backend workers without adding a session database.

Each challenge gets a fresh graph and its own evidence-retrieval state. Its only
tools are `list_evidence`, `read_evidence` and `submit_challenge`. Cited records
must be re-read during that challenge. The earlier diagnosis is a claim to
evaluate, not an answer key: the prompt permits defending, qualifying or revising
it, and labels further checks as proposed rather than executed.

The investigation proxy accepts at most 512 bytes; challenge requests allow
64 KiB for the signed diagnosis reference. Both bound uploads without a declared
length and stop reading stalled uploads after 10 seconds. Flask independently
enforces these size limits. Browser responses are validated before rendering;
missing fields, mismatched cases, foreign citations and non-JSON error pages
produce a retry state rather than broken cards. Evidence loading has a 15-second
browser deadline, investigations a 100-second browser deadline, and challenges
a 75-second browser deadline. Rapid repeat clicks cannot start duplicate work
for an already-running incident in that page.
Navigating away aborts browser requests and prevents stale UI updates, but does
not guarantee cancellation of Flask work or refund already-issued model calls.
The server's own execution deadline and concurrency limits still apply.

Submission checks structure and that cited IDs were actually retrieved. It does
**not** prove the cited evidence logically supports the claim; that requires
manual assessment against the rubric.

Each investigation model call has a 3,000-token output cap; challenge calls have
a 1,800-token cap. Both have no automatic SDK retry and a 45-second request
timeout. Investigations have a 90-second execution deadline, challenges 60 seconds,
and both have a bounded graph recursion limit. They do not automatically retry
or run additional questions. OpenAI sample runs use low reasoning effort; the
production agent's model settings remain unchanged. Token counts reported
by the provider are included in successful results and evaluation reports.

The backend requires the existing internal proxy secret and allows at most three
in-flight calls per process. Production registers the blueprint in `main_compute`
and applies a deployment-wide Redis-backed allowance of 9 POSTs/minute,
90/hour and 300/day, shared by both paid endpoints and all visitors. The standalone
loopback runner uses an explicitly process-local memory limiter. A public deployment still needs an
operator-selected spending budget, normal infrastructure timeouts and capacity
planning; this repository change does not provision hosting.

The browser displays factual explanations, source records and tool activity;
it does not display hidden model reasoning. No analytics service receives the
sample runs. The configured model provider receives the synthetic evidence.

## Verification

From `server`, run:

```sh
python -m pytest tests/services/sandbox -q
```

These tests use the actual agent graph with a deterministic model double. They
verify independent concurrent runs, isolated evidence, rejected unsupported
citations, bounded input, failures and retries. They are not a diagnostic
accuracy score.

From `client`, run the response-contract and upload-boundary tests without a
browser, running server, model credentials or paid requests:

```sh
npx playwright test --config tests/sandbox.config.ts
```

For one live attempt per case, run from the repository root:

```sh
python scripts/evaluate_sandbox.py --output /tmp/aurora-live-results.json
# To retry only one failed case and retain a separate report:
python scripts/evaluate_sandbox.py --case case-b --output /tmp/aurora-case-b-retry.json
```

This records all three attempts, including failures, model, elapsed time, token
usage and evidence hashes. It does not retry until an attractive answer appears.
Review results against `server/tests/services/sandbox/rubric.json` and retain
failed attempts. Report observed timing and the number of runs; do not claim a
general success rate from these three curated examples.

## Remaining growth validation

Test whether completing investigations and challenges leads to more installations
and first results on users' own systems. This implementation adds no analytics
service. Visitor attribution, hosted signup and completed-installation conversion
measurement are not implemented or verified by the local sandbox.

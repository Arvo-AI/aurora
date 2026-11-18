# Investigation sandbox: validation and limits

Recorded November 18, 2026. Setup, architecture and reproduction instructions:
[sandbox README](../../server/services/sandbox/README.md).

## What works

Public `/sandbox` runs three independent investigations of authored synthetic
incidents sharing one alert but different causes. Live agents retrieve records
and submit structured diagnoses. Visitors can inspect citations, then choose a
fixed challenge about alternatives, distinguishing evidence, or falsification.
Answers are generated live; the answer rubric is not exposed to the investigator.

The sandbox uses Aurora's shared graph factory and provider factory. It does not
run the full production RCA workflow, customer connectors, tenant memory or
remediation. No customer systems are accessed.

## Observed measurements

The latest three local investigations completed in **19.1, 21.0 and 22.4 seconds**,
each reading its own six records. These are server-reported processing durations,
excluding page loading, network overhead, reading and human evaluation.

| Live challenge | Duration | Records read |
|---|---:|---:|
| Incident A: competing causes | 20.0 s | 6 |
| Incident B: distinguishing evidence | 16.2 s | 6 |
| Incident C: falsification | 19.4 s | 6 |
| Incident B: competing causes, after concision changes | 14.4 s | 4 |

Initial challenge replies were verbose. The follow-up used a standalone challenge
prompt, shorter field limits and a 1,800-token output cap per model call. It
returned a concise comparison with citations and acknowledged evidence gaps.
All four observed challenge attempts completed. This small functional check does
not establish general diagnostic accuracy or production MTTR improvement.

## Automated checks

- **89 backend checks passed**, including sandbox tests, adjacent workflow
  regression tests and the connector authorization architecture check.
- **29 client/request tests passed**, without a browser or paid model calls.
- Focused TypeScript and lint checks passed; whitespace checks passed.
- The CI test install includes the real graph/runtime and limiter required by
  the new tests, matching the production graph/core pins. Hosted CI still needs
  to run after the branch is pushed.
- A changed-file credential-pattern scan found no matches. Local environment
  files are ignored and untracked.
- Repository-wide TypeScript still reports 32 errors in unchanged source and
  generated route checks. No error references the sandbox or middleware. The
  default lint configuration also has the previously observed minimatch import
  incompatibility; focused lint used the installed rules without changing that
  unrelated dependency configuration. Full production build/CI is not certified.

Reproduce the automated checks:

```sh
cd server
python -m pytest tests/services/sandbox tests/chat/test_workflow_thinking_isolation.py tests/architectural/test_connector_rbac.py -q
cd ../client
npx playwright test --config tests/sandbox.config.ts
```

For a paid live investigation report, from the repository root:

```sh
python scripts/evaluate_sandbox.py --output /tmp/aurora-live-results.json
```

Configure the server's model provider as described in the README. Start the
standalone backend and frontend, run a comparison, then select a challenge below
each diagnosis. Clicking an answered question reuses that comparison's response.

## Security and failure cases covered

- Only the four exact sandbox paths bypass account setup. Descendant paths do
  not inherit anonymous access; the public page gets the app's browser policy.
- Backend calls require the internal proxy secret. Public requests cannot choose
  customer identity, provider credentials, model, arbitrary prompt or case path.
- Signed, one-hour run references bind the original diagnosis, run ID, case and
  evidence hash. Altered, malformed, expired and stale-evidence references fail
  closed before model work, including a change between verification and startup.
- Challenge tools can only list/read that incident's evidence and submit an
  answer. Attempts to invoke shell, file or network tools are rejected.
- Citations must reference evidence read within that challenge. The model is
  allowed to qualify or revise the original diagnosis.
- Input sizes, upload time, output lengths, graph recursion, execution time,
  concurrency and paid request frequency are bounded. Both paid endpoints share
  the same allowance: 9/minute, 90/hour, 300/day.
- Failures do not cancel other incidents. Retry and stale-response checks prevent
  old answers from overwriting a new comparison. Model text is rendered as text.
- Structured output and citation checks do not prove that an explanation is
  logically correct; review the actual evidence and rubric.

## Remaining work

The experience runs locally. A public hosted deployment, production load testing,
operator spending controls and completed-installation attribution are not
provisioned. Browser cancellation does not guarantee cancellation or a refund for
already-issued model work. The standalone limiter is process-local; production
uses the existing Redis-backed limiter. A rate allowance is not a currency cap.

No visitor-level time to value or conversion uplift is claimed. Next, observe a
small group of SREs evaluating the sandbox and instrument source inspection,
challenge completion, installation click, confirmed installation and first result
on the team's own environment. The existing hosted GitHub/Slack signup callbacks
cannot be validated by this local runner.

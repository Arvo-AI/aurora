# RCA context delivery: reproduction and repair progress

Original baseline: `8cf9573`; stage-0 test commit: `39eab99`.
Stages 1-3 fix batch formatting, queue atomicity and context persistence.
No model API calls, production incidents, or account changes are needed to
run the tests.

## Run locally against the existing Docker stack

From PowerShell in `E:\Projects\Aurora`:

```powershell
docker compose run --rm --no-deps --entrypoint sh -v E:/Projects/Aurora/server:/app:ro -w /app celery_worker -c 'RCA_TEST_REDIS_URL="$REDIS_URL" PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/chat/test_rca_context_updates.py -q --tb=short -p no:cacheprovider'
```

After stage 3, expect **18 passed** with no expected-failure markers remaining.
Historically, adding `--runxfail` exposed **1 failed, 14 passed** after stage 2,
**2 failed, 9 passed** after stage 1 and **3 failed, 6 passed** at stage 0.
Each strict xfail marker was removed only when its original reproduction passed.

To verify only the repaired formatting behavior and its adjacent cases:

```powershell
docker compose run --rm --no-deps --entrypoint sh -v E:/Projects/Aurora/server:/app:ro -w /app celery_worker -c 'PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/chat/test_rca_context_updates.py -k "batch_keeps or missing_fields or manual_merge_formatting or single_update" -q -p no:cacheprovider'
```

Expect **7 passed, 11 deselected**.

To verify the queue repair, including the original losing interleaving:

```powershell
docker compose run --rm --no-deps --entrypoint sh -v E:/Projects/Aurora/server:/app:ro -w /app celery_worker -c 'RCA_TEST_REDIS_URL="$REDIS_URL" PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/chat/test_rca_context_updates.py -k "queue or drain or consumers" -q -p no:cacheprovider'
```

Expect **6 passed, 12 deselected**.

To verify context persistence, non-RCA isolation and the existing trim behavior:

```powershell
docker compose run --rm --no-deps --entrypoint sh -v E:/Projects/Aurora/server:/app:ro -w /app celery_worker -c 'PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/chat/test_rca_context_updates.py -k "later_agent_model_turns or safety_trim" -q -p no:cacheprovider'
```

Expect **4 passed, 14 deselected**.

The temporary container uses the existing worker image and network with source
mounted read-only. The Redis test uses an explicitly selected URL, a random
`rca-regression-` user/session key, a short TTL, and exact-key cleanup. It does
not flush Redis or read existing queues. The database dependencies and session
status are replaced in-process. Without `RCA_TEST_REDIS_URL`, the five real
Redis cases skip. The graph case requires the real LangChain/LangGraph packages.

## 1. Later alerts in a batch lose their fields

Test: `test_batch_keeps_each_alert_and_its_fields`.

Provide two supported alert formats, each with a distinct title, body and
service. The formatter consumes the whole batch but selects the first elements
from its collected arrays. The test reports that the second alert's title,
body, service and source are missing. It also requires the output to keep
related fields together, rather than mixing fields from separate alerts.

Relevant source: `chat/background/context_updates.py`,
`_format_updates_for_prompt()`.

Existing callers include alert correlation and Jenkins deployment context.
When multiple updates accumulate before the next model call, a later alert's
evidence can disappear even though the UI record counts all consumed updates.
No claim is made that this happens on every investigation.

Stage-1 repair: extract and render each update within its own loop iteration,
then append the shared investigation instructions once. Later updates are
preserved in order, missing fields are not borrowed from another alert, and a
manual merge does not change the formatting of ordinary alerts in the batch.
The original failing assertion now passes without an xfail marker. Two added
tests cover missing-field association and mixed manual/ordinary updates.

Seven representative single-update outputs were also compared byte-for-byte
with the stage-0 formatter and were identical. Stage 1 did not change queue
consumption or model middleware. The running services have not been rebuilt or
restarted; verification uses the changed source in a temporary test container.

## 2. A concurrent enqueue can disappear during consumption

Test: `test_update_arriving_during_drain_is_not_lost`.

Use two real Redis clients. Queue A, then schedule the producer to append B
immediately after the consumer's first network round trip. With the original
`LRANGE` followed by `DELETE`, the return value contains only A and B is absent
from the remaining queue. The desired assertion is that B remains available.

Instrumentation also covers transaction pipeline execution so the test does
not prescribe whether a future minimal fix uses a transaction or a single
atomic command. No sleeps, statistical timing assumptions, or threads are
required for the reproduction.

Relevant source: `chat/background/context_updates.py`,
`drain_rca_context_updates()`.

This is a confirmed losing interleaving, not a measurement of its production
frequency. The impact is silent loss of an accepted correlated alert or
deployment update.

Stage-2 repair: execute `LRANGE` and `DELETE` within one Redis transaction
(`pipeline(transaction=True)`). Producers cannot write between the transaction's
commands. An enqueue ordered after the transaction stays queued for the next
drain; a second consumer sees an empty queue rather than the first batch.
No retries, sleeps, new Redis dependency, or queue format changes were added.

The original controlled-interleaving regression now passes without an xfail
marker. Additional real-Redis tests cover initially empty queues, one/multiple
entries in order, repeated drains, session isolation, and two consumers starting
together. The latter checks concurrent behavior with a barrier; the original
producer interleaving remains the deterministic reproduction of message loss.
These tests do not claim crash-safe or exactly-once delivery after consumption.

## 3. Context is temporary in the inner agent graph

Test: `test_context_survives_later_agent_model_turns`.

Use the complete production context helper and middleware, the real `State`
class and execution context, and the installed LangChain `create_agent` graph.
The test uses `astream_events`, as `Agent.agentic_tool_flow()` does. A scripted
model makes three turns, with a new context update arriving during the first
tool execution. The tools contain no copy of that update in their outputs.

Before the stage-3 repair:

- Turn 1 has no update, as expected.
- Turn 2 has the update, as expected.
- Turn 3 still has prior tool evidence, but no original context message.
- The outer `State` has a UI update record; this is different from retaining
  the message in the inner graph.

Relevant source: `chat/backend/agent/middleware/context_trim.py`,
`ContextSafetyMiddleware.awrap_model_call()`. Its original request override
modified the current model input, not the graph's accumulated messages.

This does not prove that all derived information is forgotten. A model can
copy part of the update into a tool argument or answer, and outer state can
retain it. It proves that subsequent calls in the same loop cannot rely on
the original context remaining available below the trim threshold.

This test covers the actual model/tool graph path, not the full Celery task,
database persistence, guardrail evaluation, or browser UI. Those boundaries
remain to be verified during later integration checks.

Stage-3 repair: prepare queued context in the existing middleware's native
`abefore_model` hook and return a message-state update for the graph reducer.
The message is now present in the second and third model requests and the
final graph history, exactly once. The helper keeps UI metadata but no longer
pre-appends the message to outer `State.messages`; `Agent.agentic_tool_flow()`
already collects final graph messages into that list.

Tests retain earlier tool evidence and check tool-call/result associations.
Two additional graph cases verify that foreground chat and background non-RCA
work do not consume or receive queued RCA context. A real message-trimming test
checks the existing token budget, retained current question and absence of
orphaned tool results. The wrapper still trims requests as before; the repair
does not promise indefinite retention beyond the context window or recovery
across worker restarts.

## Baseline regression checks

The new tests plus `test_investigation_timeout.py`, `test_session_heartbeat.py`
and `test_deepseek_provider.py` ran in the existing worker image:
**23 passed, 3 xfailed** at stage 0, **26 passed, 2 xfailed** after stage 1,
**31 passed, 1 xfailed** after stage 2 and **35 passed** after stage 3.
Stage 3 also ran `test_force_tool_choice.py` and `test_prompt_cache_control.py`:
**70 passed**, with one third-party Google auth/grpc deprecation warning.
These checks are scoped; they are not the full suite. Stages 1-3 did not perform
live Celery, browser, database write-back, or real-model verification.

## Next stages

The three source repairs are complete. Stage 4 remains: combined integration
verification and final delivery. Do not add UI, pause/resume, connectors,
retries, or a new queue framework. Stop and report at each stage.

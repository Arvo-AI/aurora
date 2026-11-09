# RCA context delivery: stage 0 reproduction

Baseline: `8cf9573`. This stage adds tests only; none of the three defects is
fixed yet. No model API calls, production incidents, or account changes are
needed to run the tests.

## Run locally against the existing Docker stack

From PowerShell in `E:\Projects\Aurora`:

```powershell
docker compose run --rm --no-deps --entrypoint sh -v E:/Projects/Aurora/server:/app:ro -w /app celery_worker -c 'RCA_TEST_REDIS_URL="$REDIS_URL" PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/chat/test_rca_context_updates.py -q --runxfail --tb=short -p no:cacheprovider'
```

At this baseline, expect **3 failed, 6 passed**, with the three assertion failures
described below. `--runxfail` deliberately exposes the known defects.

Without `--runxfail`, expect **6 passed, 3 xfailed**. Strict xfail markers keep
this test-only checkpoint usable while documenting the unfixed behavior. They
do not weaken the assertions; a repaired case becomes an unexpected pass and
fails the suite until its marker is removed in the corresponding fix stage.
Only assertion failures qualify as these expected failures.

The temporary container uses the existing worker image and network with source
mounted read-only. The Redis test uses an explicitly selected URL, a random
`rca-regression-` user/session key, a short TTL, and exact-key cleanup. It does
not flush Redis or read existing queues. The database dependencies and session
status are replaced in-process. Without `RCA_TEST_REDIS_URL`, only the Redis
race case skips. The graph case requires the real LangChain/LangGraph packages.

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

## 2. A concurrent enqueue can disappear during consumption

Test: `test_update_arriving_during_drain_is_not_lost`.

Use two real Redis clients. Queue A, then schedule the producer to append B
immediately after the consumer's first network round trip. With the current
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

## 3. Context is temporary in the inner agent graph

Test: `test_context_survives_later_agent_model_turns`.

Use the complete production context helper and middleware, the real `State`
class and execution context, and the installed LangChain `create_agent` graph.
The test uses `astream_events`, as `Agent.agentic_tool_flow()` does. A scripted
model makes three turns, with a new context update arriving during the first
tool execution. The tools contain no copy of that update in their outputs.

At this baseline:

- Turn 1 has no update, as expected.
- Turn 2 has the update, as expected.
- Turn 3 still has prior tool evidence, but no original context message.
- The outer `State` has a UI update record; this is different from retaining
  the message in the inner graph.

Relevant source: `chat/backend/agent/middleware/context_trim.py`,
`ContextSafetyMiddleware.awrap_model_call()`. Its request override modifies
the current model input, not the graph's accumulated messages. The final
graph history assertion will also be exercised once the earlier turn-3
assertion passes after a repair.

This does not prove that all derived information is forgotten. A model can
copy part of the update into a tool argument or answer, and outer state can
retain it. It proves that subsequent calls in the same loop cannot rely on
the original context remaining available below the trim threshold.

This test covers the actual model/tool graph path, not the full Celery task,
database persistence, guardrail evaluation, or browser UI. Those boundaries
remain to be verified during later integration checks.

## Baseline regression checks

The new tests plus `test_investigation_timeout.py`, `test_session_heartbeat.py`
and `test_deepseek_provider.py` ran in the existing worker image:
**23 passed, 3 xfailed**. These checks are scoped; they are not the full suite.

## Next stages

Fix each defect separately, remove only its strict xfail marker after the
original reproduction passes, and preserve the other expected failures until
their respective repairs. Do not add UI, pause/resume, connectors, retries, or
a new queue framework as part of these fixes. Stop and report at each stage.

# RCA context delivery

## Problem and fix

An ongoing RCA could acknowledge new evidence without retaining it for the agent.
This affects correlated alerts, Jenkins deployment updates and manual merges.
The fixes change two production files:

- **Batch formatting:** queue two alerts with different fields before the next
  model call. The old formatter omitted later alerts and could mix their fields.
  Render each update independently in `chat/background/context_updates.py`.
- **Queue consumption:** append B between the consumer's `LRANGE` and `DELETE`.
  B was deleted without being returned. Execute both commands in one Redis
  transaction so later enqueues remain available for the next drain.
- **Graph history:** deliver an update during a tool call, then run two more
  model turns. The update appeared in the next request but disappeared afterward.
  Persist it through `ContextSafetyMiddleware.abefore_model`; let the existing
  graph collector append final messages to outer state to avoid duplicates.

The corresponding regressions are `test_batch_keeps_each_alert_and_its_fields`,
`test_update_arriving_during_drain_is_not_lost`, and
`test_context_survives_later_agent_model_turns` in
[test_rca_context_updates.py](test_rca_context_updates.py). The original cases
were reproduced before their fixes; the Redis interleaving is deterministic.

## Run the regression tests

With the local Docker stack available, run from the repository root:

```powershell
docker compose run --rm --no-deps --entrypoint sh -v ./server:/app:ro -w /app celery_worker -c 'RCA_TEST_REDIS_URL="$REDIS_URL" PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/chat/test_rca_context_updates.py -q -p no:cacheprovider'
```

Expected: **19 passed, 1 skipped**. No model credentials are required. The live
model case requires explicit `RCA_TEST_LIVE_MODEL=1` opt-in. Redis cases use
random temporary keys and exact-key cleanup; without `RCA_TEST_REDIS_URL`, they
skip. Graph cases require the installed LangChain/LangGraph dependencies.

## Verification evidence

- The broader related regression run passed **179 tests**, with the live model
  case skipped. Environment-variable validation also passed.
- Combined real-Redis/graph coverage retains four context identifiers across
  four model turns and three tool calls, with two UI batches and no duplicates.
  The opt-in DeepSeek run passed the same scenario using synthetic evidence.
- After rebuilding the backend, a real local Celery investigation completed.
  Saved database history contained all four supplied identifiers exactly once;
  two UI update records were saved, and the authenticated session API returned
  HTTP 200 with all four identifiers. Notifications were disabled and diagnostic
  tools were read-only; no business incident was created.

## Limits

Component tests replace database/session dependencies. The separate local runtime
check covers Celery, database persistence and the session API, but does not verify
third-party webhooks, authenticated browser rendering, production cloud queries,
cross-tenant denial or worker-failure recovery. Atomic queue consumption does not
provide crash-safe exactly-once processing. Retention remains subject to normal
context trimming; production diagnosis accuracy and time savings were not measured.

#!/usr/bin/env bash
# Phase 5 + 6 smoke test for Aurora.
#
# Exercises the SSE transport, chat_events durability, terminal idempotency,
# active_stream_id race-fix, the /api/chat/cancel endpoint, the tool-cancel
# ContextVar, the idle watchdog, and dedup invariants.
#
# Conventions:
#   - Everything runs inside Docker (per repo convention).
#   - Each section prints PASS or FAIL.
#   - Exits 1 on any FAIL; 0 if all sections pass.
#
# Usage:
#   bash scripts/smoke_test_phase5_6.sh
set -euo pipefail

SERVER="aurora-server"
PG="aurora-postgres"
REDIS="aurora-redis"

PASS=0
FAIL=0
FAILED_SECTIONS=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

section() {
  echo ""
  echo "=========================================================="
  echo " $1"
  echo "=========================================================="
}

pass() {
  echo "  PASS: $1"
  PASS=$((PASS + 1))
}

fail() {
  echo "  FAIL: $1"
  FAIL=$((FAIL + 1))
  FAILED_SECTIONS+=("$1")
}

psql_query() {
  docker exec "$PG" psql -U aurora -d aurora_db -t -A -c "$1" 2>/dev/null
}

server_python() {
  # Run a Python snippet inside aurora-server with /app on sys.path.
  docker exec -i "$SERVER" python -c "$1"
}

redis_cmd() {
  docker exec "$REDIS" redis-cli "$@"
}

# ---------------------------------------------------------------------------
# 1. Health
# ---------------------------------------------------------------------------
section "1. Health — SSE endpoint reachable"

# Internal health check via the server's own HTTP probe (-L follows the
# 308 redirect Flask emits when the canonical /health path is rewritten).
if docker exec "$SERVER" curl -sL -o /dev/null -w "%{http_code}" -m 5 http://localhost:5080/health 2>/dev/null | grep -qE "^(200|204)$"; then
  pass "aurora-server /health returns 2xx"
else
  fail "aurora-server /health unreachable"
fi

# ---------------------------------------------------------------------------
# 2. chat_events DB schema invariants
# ---------------------------------------------------------------------------
section "2. chat_events / chat_messages schema"

# parent_message_id column on chat_messages
if [[ "$(psql_query "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='chat_messages' AND column_name='parent_message_id';")" == "1" ]]; then
  pass "chat_messages.parent_message_id present"
else
  fail "chat_messages.parent_message_id missing"
fi

# uq_chat_events_terminal_per_msg partial UNIQUE
if [[ "$(psql_query "SELECT COUNT(*) FROM pg_indexes WHERE indexname='uq_chat_events_terminal_per_msg';")" == "1" ]]; then
  pass "uq_chat_events_terminal_per_msg partial UNIQUE present"
else
  fail "uq_chat_events_terminal_per_msg partial UNIQUE missing"
fi

# chat_messages.status CHECK constraint
if [[ "$(psql_query "SELECT COUNT(*) FROM pg_constraint WHERE conname='chat_messages_status_check';")" == "1" ]]; then
  pass "chat_messages.status CHECK constraint present"
else
  fail "chat_messages.status CHECK constraint missing"
fi

# ---------------------------------------------------------------------------
# 3. Terminal idempotency (UNIQUE conflict swallowed by record_event)
# ---------------------------------------------------------------------------
section "3. Terminal-event idempotency"

SESSION_ID=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
MESSAGE_ID=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
ORG_ID=$(psql_query "SELECT id FROM organizations LIMIT 1;" | head -1)

if [[ -z "$ORG_ID" ]]; then
  fail "no organization in DB to seed test session"
else
  # Seed a chat_session
  psql_query "INSERT INTO chat_sessions (id, org_id, user_id, title, created_at, updated_at) SELECT '$SESSION_ID', '$ORG_ID', (SELECT id FROM users WHERE org_id='$ORG_ID' LIMIT 1), 'smoke-test', NOW(), NOW() ON CONFLICT (id) DO NOTHING;" >/dev/null

  # Two assistant_finalized events with same (session_id, message_id) — second must collapse to seq=0.
  RESULT=$(server_python "
import asyncio
from chat.backend.agent.utils.persistence.chat_events import record_event
async def main():
    s1 = await record_event(session_id='$SESSION_ID', org_id='$ORG_ID', type='assistant_finalized',
                             payload={'reason': 'ok'}, message_id='$MESSAGE_ID')
    s2 = await record_event(session_id='$SESSION_ID', org_id='$ORG_ID', type='assistant_finalized',
                             payload={'reason': 'ok'}, message_id='$MESSAGE_ID')
    print(f'{s1},{s2}')
asyncio.run(main())
" 2>/dev/null | tail -1)

  if [[ "$RESULT" =~ ^[1-9][0-9]*,0$ ]]; then
    pass "second assistant_finalized collapsed to seq=0 (idempotent), got $RESULT"
  else
    fail "expected seq>0 then seq=0, got '$RESULT'"
  fi
fi

# ---------------------------------------------------------------------------
# 4. active_stream_id race-fix — terminal event clears it
# ---------------------------------------------------------------------------
section "4. active_stream_id auto-clear on terminal event"

SESSION_ID2=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
MESSAGE_ID2=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')

if [[ -n "$ORG_ID" ]]; then
  psql_query "INSERT INTO chat_sessions (id, org_id, user_id, title, active_stream_id, created_at, updated_at)
              SELECT '$SESSION_ID2', '$ORG_ID', (SELECT id FROM users WHERE org_id='$ORG_ID' LIMIT 1),
                     'smoke-test', '$SESSION_ID2:$MESSAGE_ID2', NOW(), NOW()
              ON CONFLICT (id) DO UPDATE SET active_stream_id = EXCLUDED.active_stream_id;" >/dev/null

  server_python "
import asyncio
from chat.backend.agent.utils.persistence.chat_events import record_event
async def main():
    await record_event(session_id='$SESSION_ID2', org_id='$ORG_ID', type='assistant_finalized',
                       payload={'reason': 'ok'}, message_id='$MESSAGE_ID2')
asyncio.run(main())
" >/dev/null 2>&1

  AFTER=$(psql_query "SELECT COALESCE(active_stream_id::text, 'NULL') FROM chat_sessions WHERE id='$SESSION_ID2';")
  if [[ "$AFTER" == "NULL" || "$AFTER" == "" ]]; then
    pass "active_stream_id cleared after terminal event"
  else
    fail "active_stream_id still set: '$AFTER'"
  fi
fi

# ---------------------------------------------------------------------------
# 5. /api/chat/cancel endpoint (delegated to cancel-determinism agent)
# ---------------------------------------------------------------------------
section "5. POST /api/chat/cancel"

# This section depends on the cancel-determinism agent's work landing.
# We verify the route exists and accepts POST. Full contract is tested by
# that agent's own suite.
CANCEL_STATUS=$(docker exec "$SERVER" curl -s -o /dev/null -w "%{http_code}" -m 5 \
  -X POST http://localhost:5080/api/chat/cancel \
  -H 'X-User-ID: smoke-test' -H 'X-Org-ID: smoke-test' \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"00000000-0000-0000-0000-000000000000"}' 2>/dev/null || echo "000")

# 401/403 (auth) or 202/404 are all "endpoint exists" — only 5xx or connection refused fails.
if [[ "$CANCEL_STATUS" =~ ^(202|404|400|401|403)$ ]]; then
  pass "/api/chat/cancel responds (HTTP $CANCEL_STATUS)"
else
  fail "/api/chat/cancel unexpected status $CANCEL_STATUS"
fi

# ---------------------------------------------------------------------------
# 6. Tool cancel ContextVar (delegated to impl-tool-cancel agent)
# ---------------------------------------------------------------------------
section "6. Tool cancel ContextVar"

# Verify the cancel_token module exists and exposes the expected API.
if server_python "
from chat.backend.agent.utils import cancel_token
assert hasattr(cancel_token, 'set_cancel_token') or hasattr(cancel_token, 'get_cancel_token'), 'API missing'
print('OK')
" 2>/dev/null | tail -1 | grep -q "^OK$"; then
  pass "cancel_token module loaded with expected API"
else
  fail "cancel_token module not yet present (impl-tool-cancel agent pending)"
fi

# ---------------------------------------------------------------------------
# 7. Idle watchdog — short TTL → assistant_failed event
# ---------------------------------------------------------------------------
section "7. Idle watchdog"

SESSION_ID3=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
MESSAGE_ID3=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')

if [[ -n "$ORG_ID" ]]; then
  psql_query "INSERT INTO chat_sessions (id, org_id, user_id, title, active_stream_id, created_at, updated_at)
              SELECT '$SESSION_ID3', '$ORG_ID', (SELECT id FROM users WHERE org_id='$ORG_ID' LIMIT 1),
                     'smoke-test', '$SESSION_ID3:$MESSAGE_ID3', NOW(), NOW()
              ON CONFLICT (id) DO UPDATE SET active_stream_id = EXCLUDED.active_stream_id;" >/dev/null

  # Don't set the idle key — simulate immediate expiry — and run one scan iteration.
  server_python "
import asyncio
from chat.backend.agent.utils.idle_watchdog import _scan_once
asyncio.run(_scan_once())
" >/dev/null 2>&1 || true

  HAS_FAILED=$(psql_query "SELECT COUNT(*) FROM chat_events
                            WHERE session_id='$SESSION_ID3' AND message_id='$MESSAGE_ID3'
                              AND type='assistant_failed'
                              AND payload::text LIKE '%idle_timeout%';")

  if [[ "$HAS_FAILED" == "1" ]]; then
    pass "idle watchdog wrote assistant_failed (reason=idle_timeout)"
  else
    fail "idle watchdog did not write assistant_failed for stuck message"
  fi

  AFTER3=$(psql_query "SELECT COALESCE(active_stream_id::text, 'NULL') FROM chat_sessions WHERE id='$SESSION_ID3';")
  if [[ "$AFTER3" == "NULL" || "$AFTER3" == "" ]]; then
    pass "idle watchdog cleared active_stream_id"
  else
    fail "active_stream_id still set after idle timeout: '$AFTER3'"
  fi
fi

# ---------------------------------------------------------------------------
# 8. Dedup invariants — at most one terminal event per message
# ---------------------------------------------------------------------------
section "8. Terminal-event dedup invariant"

DUPES=$(psql_query "
  SELECT COUNT(*) FROM (
    SELECT message_id, COUNT(*) c
    FROM chat_events
    WHERE type IN ('assistant_finalized','assistant_interrupted','assistant_failed')
      AND message_id IS NOT NULL
    GROUP BY message_id
    HAVING COUNT(*) > 1
  ) x;
")

if [[ "$DUPES" == "0" ]]; then
  pass "no message has duplicate terminal events"
else
  fail "$DUPES messages have duplicate terminal events"
fi

# ---------------------------------------------------------------------------
# 9. Frontend safe-fetch — TODO (no jest setup)
# ---------------------------------------------------------------------------
section "9. Frontend safe-fetch"
echo "  TODO: no jest harness in repo. Manual verification:"
echo "        - client/src/lib/safe-fetch.ts exports safeFetch + isSafeFetchTimeout"
echo "        - hooks / proxy routes import @/lib/safe-fetch and call safeFetch(...)"
echo "        - bunx tsc --noEmit passes inside aurora-frontend-1"

# ---------------------------------------------------------------------------
# 10. Multi-agent live trigger (best-effort)
# ---------------------------------------------------------------------------
section "10. Multi-agent RCA live trigger"
TRIGGER_SCRIPT="$(dirname "$0")/trigger_multi_agent_rca.sh"
if [[ -x "$TRIGGER_SCRIPT" ]]; then
  echo "  Running $TRIGGER_SCRIPT (best-effort, no fail on error)..."
  # Best-effort: don't fail the whole smoke if the trigger script blips, since
  # this section depends on a live LLM round-trip outside our control.
  if bash "$TRIGGER_SCRIPT" 2>&1 | tail -20; then
    pass "trigger_multi_agent_rca.sh ran"
  else
    echo "  NOTE: trigger_multi_agent_rca.sh exited non-zero (continuing — best-effort)."
  fi
else
  echo "  TODO: scripts/trigger_multi_agent_rca.sh not present yet (multi-agent agent pending)."
fi

# ---------------------------------------------------------------------------
# 11. SSE interactive chat — POST /api/chat/messages spawns Celery task
# ---------------------------------------------------------------------------
section "11. POST /api/chat/messages enqueues run_background_chat"

# Wiring check (always runs): the route imports run_background_chat and
# the new is_interactive parameter is accepted.
WIRING_OK=$(server_python "
from routes.chat_sse import bp
from chat.background.task import run_background_chat
import inspect
sig = inspect.signature(run_background_chat)
ok = 'is_interactive' in sig.parameters and 'message_id' in sig.parameters and 'model' in sig.parameters
print('OK' if ok else 'MISSING')
" 2>/dev/null | tail -1)
if [[ "$WIRING_OK" == "OK" ]]; then
  pass "run_background_chat accepts is_interactive + message_id + model"
else
  fail "run_background_chat signature missing interactive params (got '$WIRING_OK')"
fi

# Live LLM section — only runs with SMOKE_LIVE_LLM=true (avoids spending money
# on every smoke run).
if [[ "${SMOKE_LIVE_LLM:-false}" == "true" ]]; then
  echo "  SMOKE_LIVE_LLM=true — performing live POST /api/chat/messages and tail."
  if [[ -z "$ORG_ID" ]]; then
    fail "no organization to seed live test session"
  else
    LIVE_SID=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
    LIVE_USER=$(psql_query "SELECT id FROM users WHERE org_id='$ORG_ID' LIMIT 1;" | head -1)
    psql_query "INSERT INTO chat_sessions (id, org_id, user_id, title, created_at, updated_at)
                VALUES ('$LIVE_SID', '$ORG_ID', '$LIVE_USER', 'sse-live-smoke', NOW(), NOW())
                ON CONFLICT (id) DO NOTHING;" >/dev/null

    # POST a message. We stamp X-User-ID/X-Org-ID headers as the rest of the
    # smoke does — this assumes the deployment trusts internal-API headers.
    POST_RESP=$(docker exec "$SERVER" curl -s -m 10 \
      -X POST http://localhost:5080/api/chat/messages \
      -H "X-User-ID: $LIVE_USER" -H "X-Org-ID: $ORG_ID" \
      -H 'Content-Type: application/json' \
      -d "{\"session_id\":\"$LIVE_SID\",\"query\":\"What is 2+2? Answer briefly.\",\"mode\":\"ask\"}" \
      2>/dev/null || echo "{}")
    LIVE_MID=$(echo "$POST_RESP" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("message_id",""))' 2>/dev/null || echo "")
    if [[ -n "$LIVE_MID" ]]; then
      pass "POST /api/chat/messages returned message_id=$LIVE_MID"
    else
      fail "POST /api/chat/messages no message_id (resp=$POST_RESP)"
    fi

    # Wait up to 30s for chat_events to flow.
    for i in 1 2 3 4 5 6; do
      sleep 5
      EVENT_TYPES=$(psql_query "SELECT string_agg(DISTINCT type, ',' ORDER BY type) FROM chat_events WHERE session_id='$LIVE_SID';")
      [[ "$EVENT_TYPES" == *"assistant_finalized"* || "$EVENT_TYPES" == *"assistant_failed"* ]] && break
    done
    echo "  observed event types: $EVENT_TYPES"

    if [[ "$EVENT_TYPES" == *"assistant_started"* && "$EVENT_TYPES" == *"assistant_finalized"* ]]; then
      pass "chat_events flow: started -> finalized"
    else
      fail "chat_events flow incomplete (got '$EVENT_TYPES')"
    fi

    AFTER_LIVE=$(psql_query "SELECT COALESCE(active_stream_id::text,'NULL') FROM chat_sessions WHERE id='$LIVE_SID';")
    if [[ "$AFTER_LIVE" == "NULL" || "$AFTER_LIVE" == "" ]]; then
      pass "active_stream_id cleared after finalize"
    else
      fail "active_stream_id still set after finalize: '$AFTER_LIVE'"
    fi

    psql_query "DELETE FROM chat_events WHERE session_id='$LIVE_SID';" >/dev/null || true
    psql_query "DELETE FROM chat_messages WHERE session_id='$LIVE_SID';" >/dev/null || true
    psql_query "DELETE FROM chat_sessions WHERE id='$LIVE_SID';" >/dev/null || true
  fi
else
  echo "  TODO: SMOKE_LIVE_LLM not set; skipping live POST. Run with SMOKE_LIVE_LLM=true to exercise."
fi

# ---------------------------------------------------------------------------
# 12. SSE confirmation channel — pubsub wiring without LLM
# ---------------------------------------------------------------------------
section "12. SSE confirmation pubsub wiring"

# This proves the confirm channel pubsub plumbing works end-to-end:
#   1. seed _pending_confirmations[cid]
#   2. publish on chat:confirm:{session_id}
#   3. run the listener for ~2s
#   4. verify _pending_confirmations[cid]['result'] == 'execute'
SSE_SID=$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')
SSE_CID="smoke-cid-$(date +%s)"

CONFIRM_RESULT=$(server_python "
import asyncio, json
from utils.cloud.infrastructure_confirmation import _pending_confirmations
from chat.backend.agent.utils.sse_control_listener import listen_for_session_controls
from utils.redis.redis_stream_bus import get_async_redis

# Seed a pending confirmation manually (mimics what wait_for_user_confirmation does)
_pending_confirmations['$SSE_CID'] = {'result': None, 'user_id': 'smoke-user'}

async def main():
    stop = asyncio.Event()
    task = asyncio.create_task(listen_for_session_controls(
        session_id='$SSE_SID', user_id='smoke-user', stop_event=stop, mode='ask',
    ))
    # Give the listener a moment to subscribe
    await asyncio.sleep(0.5)

    client = await get_async_redis()
    await client.publish(
        'chat:confirm:$SSE_SID',
        json.dumps({'confirmation_id': '$SSE_CID', 'response': 'approve'}),
    )
    await client.aclose()

    # Poll until resolution or timeout
    for _ in range(20):
        if _pending_confirmations.get('$SSE_CID', {}).get('result'):
            break
        await asyncio.sleep(0.2)

    stop.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except Exception:
        task.cancel()

    print(_pending_confirmations.get('$SSE_CID', {}).get('result'))

asyncio.run(main())
" 2>/dev/null | tail -1)

if [[ "$CONFIRM_RESULT" == "execute" ]]; then
  pass "SSE confirmation 'approve' resolved -> 'execute' via pubsub"
else
  fail "SSE confirmation pubsub did not resolve (got '$CONFIRM_RESULT')"
fi

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
if [[ -n "$ORG_ID" ]]; then
  psql_query "DELETE FROM chat_events WHERE session_id IN ('$SESSION_ID', '$SESSION_ID2', '$SESSION_ID3');" >/dev/null || true
  psql_query "DELETE FROM chat_messages WHERE session_id IN ('$SESSION_ID', '$SESSION_ID2', '$SESSION_ID3');" >/dev/null || true
  psql_query "DELETE FROM chat_sessions WHERE id IN ('$SESSION_ID', '$SESSION_ID2', '$SESSION_ID3');" >/dev/null || true
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=========================================================="
echo " Summary"
echo "=========================================================="
echo "  PASS: $PASS"
echo "  FAIL: $FAIL"
if (( FAIL > 0 )); then
  echo ""
  echo "  Failed sections:"
  for s in "${FAILED_SECTIONS[@]}"; do echo "    - $s"; done
  exit 1
fi
echo "  All Phase 5 + 6 smoke checks passed."
exit 0

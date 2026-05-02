'use client';

import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import {
  ChatStreamEvent,
  MessagePart,
  reduceParts,
} from '@/lib/chat-message-parts';

// One row per (message_id, agent_id) tuple. Ordered by first-seen seq.
export interface ChatRow {
  message_id: string;
  agent_id: string;
  parent_agent_id: string | null;
  role: 'user' | 'assistant';
  parts: MessagePart[];
  firstSeq: number;
  lastSeq: number;
  status: 'streaming' | 'finalized' | 'interrupted' | 'failed';
}

interface State {
  rows: ChatRow[];
  byKey: Record<string, number>; // `${message_id}:${agent_id}` → index in rows
  lastSeq: number;
  // Per-message high-water-mark seq. Drop any event with
  // seq <= lastSeenSeqByMessage[mid] — the durable backlog and live tail can
  // both replay the same seq, so this dedup is what guarantees no duplicate
  // part lands in chat_messages.parts[]. Must be cloned (not mutated) on each
  // write — see the reducer note below for the StrictMode trap.
  lastSeenSeqByMessage: Map<string, number>;
}

type Action =
  | { kind: 'event'; evt: ChatStreamEvent }
  | { kind: 'reset'; lastSeq: number };

function rowKey(message_id: string, agent_id: string): string {
  return `${message_id}:${agent_id}`;
}

function applyEvent(state: State, evt: ChatStreamEvent): State {
  const agent_id = evt.agent_id || 'main';
  const key = rowKey(evt.message_id, agent_id);
  const existingIdx = state.byKey[key];

  // Reducer must stay pure (StrictMode double-invokes it in dev): mutating
  // state.lastSeenSeqByMessage in place lets the second invocation observe
  // seq <= lastSeenForMsg and silently drop every accepted event.
  const lastSeenForMsg = state.lastSeenSeqByMessage.get(evt.message_id) ?? 0;
  if (evt.seq <= lastSeenForMsg) {
    return { ...state, lastSeq: Math.max(state.lastSeq, evt.seq) };
  }
  const nextLastSeenByMessage = new Map(state.lastSeenSeqByMessage);
  nextLastSeenByMessage.set(evt.message_id, evt.seq);

  // user_message lays down a user-role row directly from payload.text.
  if (evt.type === 'user_message') {
    const text = (evt.payload?.text as string) ?? '';
    if (existingIdx !== undefined) {
      // Already present (replay echo of the row creation itself) — no-op.
      return {
        ...state,
        lastSeq: Math.max(state.lastSeq, evt.seq),
        lastSeenSeqByMessage: nextLastSeenByMessage,
      };
    }
    const newRow: ChatRow = {
      message_id: evt.message_id,
      agent_id,
      parent_agent_id: evt.parent_agent_id ?? null,
      role: 'user',
      parts: text ? [{ type: 'text', text, state: 'done' }] : [],
      firstSeq: evt.seq,
      lastSeq: evt.seq,
      status: 'finalized',
    };
    return {
      rows: [...state.rows, newRow],
      byKey: { ...state.byKey, [key]: state.rows.length },
      lastSeq: Math.max(state.lastSeq, evt.seq),
      lastSeenSeqByMessage: nextLastSeenByMessage,
    };
  }

  // For all other events: ensure assistant row exists, then run the parts reducer.
  if (existingIdx === undefined) {
    const newRow: ChatRow = {
      message_id: evt.message_id,
      agent_id,
      parent_agent_id: evt.parent_agent_id ?? null,
      role: 'assistant',
      parts: reduceParts([], evt),
      firstSeq: evt.seq,
      lastSeq: evt.seq,
      status: terminalFor(evt.type) ?? 'streaming',
    };
    return {
      rows: [...state.rows, newRow],
      byKey: { ...state.byKey, [key]: state.rows.length },
      lastSeq: Math.max(state.lastSeq, evt.seq),
      lastSeenSeqByMessage: nextLastSeenByMessage,
    };
  }

  const cur = state.rows[existingIdx];
  const nextParts = reduceParts(cur.parts, evt);
  const status = terminalFor(evt.type) ?? cur.status;
  const updated: ChatRow = {
    ...cur,
    parts: nextParts,
    lastSeq: evt.seq,
    status,
  };
  const rows = state.rows.slice();
  rows[existingIdx] = updated;
  return {
    rows,
    byKey: state.byKey,
    lastSeq: Math.max(state.lastSeq, evt.seq),
    lastSeenSeqByMessage: nextLastSeenByMessage,
  };
}

function terminalFor(type: string): ChatRow['status'] | null {
  if (type === 'assistant_finalized') return 'finalized';
  if (type === 'assistant_interrupted') return 'interrupted';
  if (type === 'assistant_failed') return 'failed';
  return null;
}

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case 'event':
      return applyEvent(state, action.evt);
    case 'reset':
      return { rows: [], byKey: {}, lastSeq: action.lastSeq, lastSeenSeqByMessage: new Map() };
    default:
      return state;
  }
}

const INITIAL: State = { rows: [], byKey: {}, lastSeq: 0, lastSeenSeqByMessage: new Map() };

// Persisted last-seen seq per session — survives tab close. The companion
// timestamp lets a periodic GC drop entries for sessions the user no longer
// touches; without it the LS namespace grows unbounded.
function lastSeqKey(sessionId: string): string {
  return `chat:lastSeq:${sessionId}`;
}

function lastSeqTouchKey(sessionId: string): string {
  return `chat:lastSeqAt:${sessionId}`;
}

function loadLastSeq(sessionId: string): number {
  if (typeof window === 'undefined') return 0;
  try {
    const raw = window.localStorage.getItem(lastSeqKey(sessionId));
    if (!raw) return 0;
    const n = parseInt(raw, 10);
    return Number.isFinite(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}

function saveLastSeq(sessionId: string, seq: number): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(lastSeqKey(sessionId), String(seq));
    window.localStorage.setItem(lastSeqTouchKey(sessionId), String(Date.now()));
  } catch {
    // ignore quota errors
  }
}

const LAST_SEQ_RETENTION_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

function gcLastSeqEntries(): void {
  if (typeof window === 'undefined') return;
  try {
    const cutoff = Date.now() - LAST_SEQ_RETENTION_MS;
    const stale: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (!k || !k.startsWith('chat:lastSeq:')) continue;
      const sid = k.slice('chat:lastSeq:'.length);
      const touchedRaw = window.localStorage.getItem(lastSeqTouchKey(sid));
      const touched = touchedRaw ? parseInt(touchedRaw, 10) : 0;
      if (!Number.isFinite(touched) || touched < cutoff) {
        stale.push(k);
        stale.push(lastSeqTouchKey(sid));
      }
    }
    for (const k of stale) window.localStorage.removeItem(k);
  } catch {
    // ignore quota / access errors
  }
}

// Parse a single SSE frame block into either an event payload or null
// (for comments / heartbeats / unknown frames).
function parseSSEFrame(block: string): { event?: string; data?: string; id?: string } | null {
  if (!block.trim()) return null;
  // A frame consisting only of `:`-prefixed lines is a comment (heartbeat).
  const lines = block.split('\n');
  if (lines.every((l) => l.startsWith(':') || l === '')) return null;

  const out: { event?: string; data?: string; id?: string } = {};
  const dataLines: string[] = [];
  for (const line of lines) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const field = line.slice(0, colon);
    const value = line[colon + 1] === ' ' ? line.slice(colon + 2) : line.slice(colon + 1);
    if (field === 'event') out.event = value;
    else if (field === 'data') dataLines.push(value);
    else if (field === 'id') out.id = value;
  }
  if (dataLines.length) out.data = dataLines.join('\n');
  return out;
}

export interface UseChatStreamOptions {
  sessionId: string | null;
  enabled?: boolean;
  onMetaCompleted?: () => void;
  onMetaResumed?: () => void;
  // Side-channel for transient UI signals that don't belong on a message
  // (e.g. toast_notification, usage_update, usage_final). Fires per matching
  // event; the parts reducer does not observe these.
  onToast?: (payload: Record<string, unknown>) => void;
  onUsageUpdate?: (payload: Record<string, unknown>) => void;
  onUsageFinal?: (payload: Record<string, unknown>) => void;
}

export interface UseChatStreamResult {
  rows: ChatRow[];
  lastSeq: number;
  connected: boolean;
  reset: () => void;
}

const MAX_RETRIES = 10;
const BASE_BACKOFF_MS = 250;
const MAX_BACKOFF_MS = 5_000;
// Idle reconnect cadence between turns: backend returns 204 when no turn is
// active. Picked to balance "next send shows up promptly" vs. "don't slam
// Flask with one GET per stream every second." With multiple tabs open each
// polling on its own session, lower values measurably starved POST handlers
// of threads and the proxy started timing out.
const IDLE_POLL_MS = 4_000;

export function useChatStream({
  sessionId,
  enabled = true,
  onMetaCompleted,
  onMetaResumed,
  onToast,
  onUsageUpdate,
  onUsageFinal,
}: UseChatStreamOptions): UseChatStreamResult {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const [connected, setConnected] = useState(false);

  // Latest callbacks via ref to avoid resubscribing.
  const onCompletedRef = useRef(onMetaCompleted);
  const onResumedRef = useRef(onMetaResumed);
  const onToastRef = useRef(onToast);
  const onUsageUpdateRef = useRef(onUsageUpdate);
  const onUsageFinalRef = useRef(onUsageFinal);
  useEffect(() => {
    onCompletedRef.current = onMetaCompleted;
    onResumedRef.current = onMetaResumed;
    onToastRef.current = onToast;
    onUsageUpdateRef.current = onUsageUpdate;
    onUsageFinalRef.current = onUsageFinal;
  }, [onMetaCompleted, onMetaResumed, onToast, onUsageUpdate, onUsageFinal]);

  // Track lastSeq via ref so reconnect logic always reads the latest.
  const lastSeqRef = useRef<number>(0);
  useEffect(() => {
    lastSeqRef.current = state.lastSeq;
    if (sessionId && state.lastSeq > 0) saveLastSeq(sessionId, state.lastSeq);
  }, [state.lastSeq, sessionId]);

  const reset = useCallback(() => {
    dispatch({ kind: 'reset', lastSeq: 0 });
    if (sessionId) {
      try {
        window.localStorage.removeItem(lastSeqKey(sessionId));
        // Drop the touch timestamp too so GC sees a fully cleared entry.
        window.localStorage.removeItem(lastSeqTouchKey(sessionId));
      } catch { /* ignore */ }
    }
  }, [sessionId]);

  useEffect(() => {
    if (!enabled || !sessionId) return;

    // GC stale per-session lastSeq entries on mount so localStorage doesn't
    // accumulate keys for chats the user hasn't touched in 30 days.
    gcLastSeqEntries();

    // Seed lastSeq from localStorage so a fresh mount can resume.
    const seeded = loadLastSeq(sessionId);
    if (seeded > 0) {
      dispatch({ kind: 'reset', lastSeq: seeded });
      lastSeqRef.current = seeded;
    } else {
      dispatch({ kind: 'reset', lastSeq: 0 });
      lastSeqRef.current = 0;
    }

    let cancelled = false;
    let retries = 0;
    const ac = new AbortController();

    const connect = async () => {
      while (!cancelled && retries <= MAX_RETRIES) {
        try {
          const headers: Record<string, string> = { Accept: 'text/event-stream' };
          if (lastSeqRef.current > 0) {
            headers['Last-Event-ID'] = String(lastSeqRef.current);
          }

          const res = await fetch(
            `/api/chat/stream?session_id=${encodeURIComponent(sessionId)}`,
            { method: 'GET', headers, signal: ac.signal, cache: 'no-store' },
          );

          if (res.status === 204) {
            // No active turn — idle poll. Don't burn the retry budget here;
            // 204 is the steady state between turns in the same session, so
            // counting it against MAX_RETRIES would silently kill the
            // consumer after ~10 idle ticks and break follow-up sends.
            setConnected(false);
            await sleep(IDLE_POLL_MS);
            continue;
          }
          if (!res.ok || !res.body) {
            throw new Error(`SSE connect failed: ${res.status}`);
          }

          setConnected(true);
          retries = 0;

          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          while (!cancelled) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            // SSE frames are separated by `\n\n`.
            let sep = buffer.indexOf('\n\n');
            while (sep >= 0) {
              const block = buffer.slice(0, sep);
              buffer = buffer.slice(sep + 2);
              const frame = parseSSEFrame(block);
              if (frame && (frame.event || frame.data)) {
                handleFrame(frame);
              }
              sep = buffer.indexOf('\n\n');
            }
          }

          // Stream ended cleanly — meta:completed for this turn. Keep the
          // consumer alive: a follow-up message in the same session needs the
          // next SSE GET to deliver its events. Without resetting retries the
          // 204-backoff path would trip MAX_RETRIES and silently quit.
          setConnected(false);
          retries = 0;
          continue;
        } catch (err) {
          if (cancelled || (err as Error)?.name === 'AbortError') return;
          setConnected(false);
          retries += 1;
          if (retries > MAX_RETRIES) return;
          await sleep(backoff(retries));
        }
      }
    };

    const handleFrame = (frame: { event?: string; data?: string; id?: string }) => {
      const evType = frame.event || 'message';

      if (evType === 'meta:completed') {
        if (frame.id) lastSeqRef.current = Math.max(lastSeqRef.current, parseInt(frame.id, 10) || 0);
        onCompletedRef.current?.();
        return;
      }
      if (evType === 'meta:resumed') {
        onResumedRef.current?.();
        return;
      }

      if (!frame.data) return;
      let parsed: ChatStreamEvent;
      try {
        parsed = JSON.parse(frame.data) as ChatStreamEvent;
      } catch {
        return;
      }

      // Prefer the explicit `id` from the SSE frame for resume bookkeeping;
      // fall back to the seq inside the data envelope.
      const seqFromId = frame.id ? parseInt(frame.id, 10) : NaN;
      const evt: ChatStreamEvent = {
        ...parsed,
        seq: Number.isFinite(seqFromId) ? seqFromId : parsed.seq,
        type: parsed.type ?? evType,
      };
      // Transient signals — fire side-channel, skip parts dispatch (they don't
      // belong on a message). Advance lastSeqRef so a reconnect with
      // Last-Event-ID doesn't redeliver them from the durable stream.
      if (
        evt.type === 'toast_notification' ||
        evt.type === 'usage_update' ||
        evt.type === 'usage_final'
      ) {
        // Prefer the SSE frame's id; fall back to the seq inside the envelope
        // so transients without a wire-level id still bump lastSeqRef.
        const seq = Number.isFinite(seqFromId) ? seqFromId : Number(evt.seq);
        if (Number.isFinite(seq)) {
          lastSeqRef.current = Math.max(lastSeqRef.current, seq);
        }
        const payload = evt.payload || {};
        if (evt.type === 'toast_notification') onToastRef.current?.(payload);
        else if (evt.type === 'usage_update') onUsageUpdateRef.current?.(payload);
        else onUsageFinalRef.current?.(payload);
        return;
      }
      dispatch({ kind: 'event', evt });
    };

    connect();

    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [enabled, sessionId]);

  return {
    rows: state.rows,
    lastSeq: state.lastSeq,
    connected,
    reset,
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms));
}

function backoff(attempt: number): number {
  const exp = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempt);
  // small jitter to avoid thundering herd
  return Math.floor(exp * (0.7 + Math.random() * 0.6));
}

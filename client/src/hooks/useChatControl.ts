'use client';

import { useCallback } from 'react';

// Control-plane POSTs that complement useChatStream. Each helper hits the
// Next.js proxy which forwards to Flask via forwardRequest (auth + RBAC).

export interface SendMessageInput {
  session_id: string;
  query: string;
  mode?: string;
  attachments?: unknown[];
  model?: string;
  // The Flask handler accepts both shapes; arrays preserve multi-provider
  // selection. Joining to a comma-string here would land as a single bogus
  // entry like ["aws,gcp"] in chat_sse.py:403-407.
  provider_preference?: string | string[];
  selected_project_id?: string;
  direct_tool_call?: unknown;
  ui_state?: Record<string, unknown>;
  // True when the user hit the RCA toggle before sending. The WS path picks
  // this up via main_chatbot data.get('trigger_rca'); SSE forwards it to
  // chat_sse.post_message which threads it into State.trigger_rca_requested.
  trigger_rca?: boolean;
}

export interface SendMessageResult {
  message_id: string;
  stream_url: string;
  session_id: string;
}

// Plain fetch with a single transparent retry on Bun's stale-keepalive hang.
// The hang surfaces either as a client-side AbortError (when our local fetch
// times out) or as a proxy 504 with body "Request timeout for ..." (when the
// proxy → Flask leg wedges on a stale pooled socket). Either way, retrying
// forces Bun to open a fresh connection and the second write succeeds. The
// proxy adds `Connection: close` for non-GET to make this rare in practice.
async function postJson<T>(url: string, body: unknown, retried = false): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });
  } catch (err) {
    if (!retried && (err as Error)?.name === 'AbortError') {
      return postJson<T>(url, body, true);
    }
    throw err;
  }

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    if (!retried && res.status === 504 && text.includes('Request timeout')) {
      return postJson<T>(url, body, true);
    }
    throw new Error(text || `Request failed: ${res.status}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

export function useChatControl() {
  const sendMessage = useCallback((input: SendMessageInput): Promise<SendMessageResult> => {
    return postJson<SendMessageResult>('/api/chat/messages', input);
  }, []);

  const cancel = useCallback(
    (session_id: string, message_id?: string): Promise<void> => {
      return postJson<void>('/api/chat/cancel', { session_id, message_id });
    },
    [],
  );

  const respondToConfirmation = useCallback(
    (session_id: string, confirmation_id: string, response: unknown): Promise<void> => {
      return postJson<void>('/api/chat/confirmations', {
        session_id,
        confirmation_id,
        response,
      });
    },
    [],
  );

  const triggerDirectTool = useCallback(
    (session_id: string, tool_call_payload: unknown): Promise<void> => {
      return postJson<void>('/api/chat/direct-tool', {
        session_id,
        tool_call_payload,
      });
    },
    [],
  );

  return { sendMessage, cancel, respondToConfirmation, triggerDirectTool };
}

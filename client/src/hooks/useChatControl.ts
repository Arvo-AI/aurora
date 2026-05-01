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
  provider_preference?: string;
  selected_project_id?: string;
  direct_tool_call?: unknown;
  ui_state?: Record<string, unknown>;
}

export interface SendMessageResult {
  message_id: string;
  stream_url: string;
  session_id: string;
}

// Plain fetch — proxy's safe-fetch already enforces a Promise.race timeout.
async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
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

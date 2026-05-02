import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  // The cancel POST is small and Flask handles it cheaply (it just publishes
  // to Redis), but Bun's keepalive pool can wedge the proxy → Flask leg the
  // same way it does for /messages. Give it the same generous budget so the
  // user's Stop click doesn't silently 500 at 30s, and let useChatControl's
  // postJson retry-once-on-AbortError ride out a stale socket.
  return forwardRequest(request, 'POST', '/api/chat/cancel', 'chat/cancel', {
    timeoutMs: 60_000,
  });
}

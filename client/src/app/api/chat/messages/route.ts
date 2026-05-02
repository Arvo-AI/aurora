import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  // Tool-heavy chats (github_commit, multi-step agent loops) can keep the
  // backend busy long past the global 30s default while SSE streams chunks
  // separately. Keep the proxy POST alive long enough that the ack lands
  // even under load — otherwise we tear down the optimistic UI for a turn
  // the backend already accepted.
  return forwardRequest(request, 'POST', '/api/chat/messages', 'chat/messages', {
    timeoutMs: 120_000,
  });
}

// Read-side projection of `chat_messages` for a given session_id. The Flask
// backend exposes this for hydrating the parts[] array on session load; if it
// is not yet wired, the client falls back to SSE replay via Last-Event-ID.
export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/chat/messages', 'chat/messages');
}

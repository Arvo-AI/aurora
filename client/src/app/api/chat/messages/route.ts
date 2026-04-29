import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/chat/messages', 'chat/messages');
}

// Read-side projection of `chat_messages` for a given session_id. The Flask
// backend exposes this for hydrating the parts[] array on session load; if it
// is not yet wired, the client falls back to SSE replay via Last-Event-ID.
export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/chat/messages', 'chat/messages');
}

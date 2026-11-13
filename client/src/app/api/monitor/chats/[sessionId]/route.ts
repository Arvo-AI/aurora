import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await params;
  return forwardRequest(
    request,
    'GET',
    `/api/monitor/chats/${encodeURIComponent(sessionId)}`,
    'Failed to load chat',
  );
}

import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/connectors/status', 'connectors status', { timeoutMs: 20_000 });
}

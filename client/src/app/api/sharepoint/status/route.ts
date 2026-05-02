import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/sharepoint/status', 'sharepoint status', { timeoutMs: 15_000 });
}

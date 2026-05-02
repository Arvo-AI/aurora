import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/splunk/status', 'splunk status', { timeoutMs: 15_000 });
}

import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/notion/connect', 'notion connect', { timeoutMs: 15_000 });
}

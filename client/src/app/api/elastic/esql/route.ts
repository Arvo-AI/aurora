import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/elastic/esql', 'elastic/esql', { timeoutMs: 90_000 });
}

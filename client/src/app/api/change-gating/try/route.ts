import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Start a zero-connection preview review (public PR URL or pasted diff).
export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/change-gating/try', 'change-gating-try');
}

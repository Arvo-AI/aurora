import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/gcp/post-auth-retry', 'gcp-post-auth-retry');
}

export { handler as POST };

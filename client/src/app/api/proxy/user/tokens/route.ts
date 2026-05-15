import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/api/user/tokens', 'user-tokens');
}

export { handler as GET, handler as POST };

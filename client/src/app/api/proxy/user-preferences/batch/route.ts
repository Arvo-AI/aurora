import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/api/user-preferences/batch', 'user-preferences-batch');
}

export { handler as GET, handler as POST };

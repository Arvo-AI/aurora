import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/azure/subscriptions', 'azure-subscriptions');
}

export { handler as POST };

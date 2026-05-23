import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/billing/usage', 'billing-usage');
}

export { handler as GET };

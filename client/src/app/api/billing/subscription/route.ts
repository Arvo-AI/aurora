import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/billing/subscription', 'billing-subscription');
}

export { handler as GET };

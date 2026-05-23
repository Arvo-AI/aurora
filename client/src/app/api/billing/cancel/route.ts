import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/billing/cancel', 'billing-cancel');
}

export { handler as POST };

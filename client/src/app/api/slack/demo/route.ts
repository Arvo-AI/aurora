import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(request: NextRequest) {
  return forwardRequest(request, 'POST', '/slack/demo', 'slack-demo', { passBody: false });
}

export { handler as POST };

import { NextRequest } from 'next/server';
import { forwardPublicRequest } from '@/lib/backend-proxy';

// Public: the sign-in page asks whether to show "Continue with Slack".
async function handler(request: NextRequest) {
  return forwardPublicRequest(request, 'GET', '/slack/signin/available', 'slack-signin-available', { timeoutMs: 5000 });
}

export { handler as GET };

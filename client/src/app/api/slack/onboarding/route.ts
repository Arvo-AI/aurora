import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Proxies /api/slack/onboarding to the Flask backend /slack/onboarding, which
// folds the team's onboarding answers into the "Slack" memory.
async function handler(request: NextRequest) {
  return forwardRequest(request, request.method, '/slack/onboarding', 'slack');
}

export { handler as POST };

import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// POST /api/incident-replay/run → run one replay and return the validated result.
// The investigation is synchronous: the backend waits for the model, validates
// every citation against the bundle, and answers with both.
export const maxDuration = 300;

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/incident-replay/run', 'Failed to run replay');
}

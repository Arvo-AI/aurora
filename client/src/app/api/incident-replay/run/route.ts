import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// POST /api/incident-replay/run → run one replay and return the validated result.
// The investigation is synchronous: the backend waits for the model, validates
// every citation against the bundle, and answers with both.
export const maxDuration = 300;

export async function POST(request: NextRequest) {
  // forwardRequest defaults to a 30s timeout, which a real investigation exceeds:
  // measured runs land between 15 and 50 seconds, so the default would abort mid
  // call and surface a 504 to a user who did nothing wrong. The backend enforces
  // its own deadline, so this only has to be larger than that.
  return forwardRequest(request, 'POST', '/api/incident-replay/run', 'Failed to run replay', {
    timeoutMs: 240_000,
  });
}

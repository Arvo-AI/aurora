import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Creates an incident from pasted evidence and starts a background RCA.
export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/incidents/trigger-rca', 'trigger-rca');
}

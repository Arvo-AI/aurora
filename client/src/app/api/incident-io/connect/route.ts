import { NextRequest } from 'next/server';
import { forwardAuthenticatedRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  const body = await request.json();
  return forwardAuthenticatedRequest('/incidentio/connect', 'incident-io/connect', {
    method: 'POST',
    body,
  });
}

import { NextRequest } from 'next/server';
import { forwardAuthenticatedRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  return forwardAuthenticatedRequest('/incidentio/alerts', 'incident-io/alerts', {
    request,
  });
}

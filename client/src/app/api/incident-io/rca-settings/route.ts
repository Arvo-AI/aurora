import { NextRequest } from 'next/server';
import { forwardAuthenticatedRequest } from '@/lib/backend-proxy';

export async function GET() {
  return forwardAuthenticatedRequest('/incidentio/rca-settings', 'incident-io/rca-settings');
}

export async function PUT(request: NextRequest) {
  const body = await request.json();
  return forwardAuthenticatedRequest('/incidentio/rca-settings', 'incident-io/rca-settings', {
    method: 'PUT',
    body,
  });
}

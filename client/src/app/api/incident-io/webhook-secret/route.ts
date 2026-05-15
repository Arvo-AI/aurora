import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function PUT(request: NextRequest) {
  return forwardRequest(request, 'PUT', '/incidentio/webhook-secret', 'incident-io/webhook-secret');
}

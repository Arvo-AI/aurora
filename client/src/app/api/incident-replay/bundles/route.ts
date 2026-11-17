import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// GET /api/incident-replay/bundles → the evidence bundles this deployment ships
export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/incident-replay/bundles', 'Failed to list evidence bundles');
}

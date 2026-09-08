import { NextRequest } from 'next/server';
import { forwardAuthenticatedGet } from '@/lib/backend-proxy';

// Query string (?groups=1, limit, offset, status) is forwarded to Flask.
export async function GET(request: NextRequest) {
  return forwardAuthenticatedGet(request, '/api/incidents', 'incidents');
}

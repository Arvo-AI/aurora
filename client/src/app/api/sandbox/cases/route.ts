import { NextRequest } from 'next/server';
import { forwardPublicRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest) {
  const response = await forwardPublicRequest(request, 'GET', '/sandbox/cases', 'sample evidence');
  response.headers.set('Cache-Control', 'no-store');
  return response;
}

import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest) {
  const response = await forwardRequest(request, 'POST', '/api/ci-diagnosis', 'CI log diagnosis', { timeoutMs: 55_000 });
  response.headers.set('Cache-Control', 'no-store');
  return response;
}

import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const backendPath = `/splunk-on-call/${path.join('/')}`;
  return forwardRequest(request, request.method, backendPath, 'Splunk On-Call');
}

export const GET = proxy;
export const POST = proxy;
export const DELETE = proxy;

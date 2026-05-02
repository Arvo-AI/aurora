import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function proxyToBackend(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const subPath = path.join('/');
  return forwardRequest(request, request.method, `/spinnaker/${subPath}`, `spinnaker/${subPath}`, { timeoutMs: 15_000 });
}

export const GET = proxyToBackend;
export const POST = proxyToBackend;
export const PUT = proxyToBackend;
export const DELETE = proxyToBackend;

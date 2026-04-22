import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const backendPath = `/api/gcp/cloud-graph/${path.map(encodeURIComponent).join('/')}`;
  return forwardRequest(request, request.method, backendPath, 'gcp-cloud-graph');
}

export { handler as POST };

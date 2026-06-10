import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const backendPath = '/api/kubeconfig/' + path.join('/');
  return forwardRequest(request, request.method, backendPath, 'kubeconfig');
}

export { handler as GET, handler as POST, handler as DELETE };

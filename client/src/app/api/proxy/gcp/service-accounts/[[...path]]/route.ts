import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path } = await params;
  const suffix = path && path.length > 0 ? '/' + path.join('/') : '';
  const backendPath = '/api/gcp/service-accounts' + suffix;
  return forwardRequest(request, request.method, backendPath, 'gcp');
}

export { handler as GET, handler as POST, handler as DELETE };

import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const backendPath = '/azure/' + path.join('/');
  return forwardRequest(request, request.method, backendPath, 'azure');
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };

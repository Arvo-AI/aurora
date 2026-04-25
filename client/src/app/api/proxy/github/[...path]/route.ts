import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const backendPath = '/github/' + path.join('/');
  return forwardRequest(request, request.method, backendPath, 'github');
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };

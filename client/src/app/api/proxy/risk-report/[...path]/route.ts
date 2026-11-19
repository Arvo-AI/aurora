import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forwardRequest(request, request.method, '/risk-report/' + path.join('/'), 'risk-report');
}

export { handler as GET, handler as POST };

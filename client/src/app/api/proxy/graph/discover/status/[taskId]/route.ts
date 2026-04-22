import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await params;
  const backendPath = `/api/graph/discover/status/${encodeURIComponent(taskId)}`;
  return forwardRequest(request, request.method, backendPath, 'graph-discover-status');
}

export { handler as GET };

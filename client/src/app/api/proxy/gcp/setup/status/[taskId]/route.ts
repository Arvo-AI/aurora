import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await params;
  const backendPath = `/gcp/setup/status/${encodeURIComponent(taskId)}`;
  return forwardRequest(request, request.method, backendPath, 'gcp-setup-status');
}

export { handler as GET };

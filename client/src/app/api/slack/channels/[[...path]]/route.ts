import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Proxies /api/slack/channels/** to the Flask backend /slack/channels/**.
// Kept separate from /api/slack (base connect/status/disconnect) so the
// channel-management endpoints (list/refresh/dismiss/restore/metadata/card-channel)
// are reachable through the required backend proxy boundary.
async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path } = await params;
  const suffix = path && path.length ? '/' + path.join('/') : '';
  const backendPath = '/slack/channels' + suffix;
  return forwardRequest(request, request.method, backendPath, 'slack');
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };

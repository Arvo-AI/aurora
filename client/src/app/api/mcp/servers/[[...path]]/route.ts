import { NextRequest, NextResponse } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Customer-registered MCP servers. Scoped under /api/mcp/servers so it cannot
// shadow /api/mcp/tokens, which serves Aurora's own MCP access tokens.
// Registration probes a remote server, so it needs longer than the 30s default.
const PROBE_TIMEOUT_MS = 45_000;

async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path = [] } = await params;
  const suffix = path.length ? `/${path.join('/')}` : '';
  return forwardRequest(
    request,
    request.method,
    `/mcp/servers${suffix}`,
    'mcp servers',
    { timeoutMs: PROBE_TIMEOUT_MS },
  );
}

export const GET = proxy;
export const POST = proxy;
export const DELETE = proxy;

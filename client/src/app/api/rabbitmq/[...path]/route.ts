import { NextRequest, NextResponse } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

const ALLOWED = new Set(['connect', 'status', 'disconnect']);

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  if (path.length !== 1 || !ALLOWED.has(path[0])) {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
  }
  return forwardRequest(request, request.method, `/rabbitmq/${path[0]}`, 'rabbitmq', {
    timeoutMs: 30_000,
  });
}

export const GET = handler;
export const POST = handler;
export const DELETE = handler;

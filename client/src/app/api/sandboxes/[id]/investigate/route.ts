import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return forwardRequest(request, 'POST', `/api/sandboxes/${encodeURIComponent(id)}/investigate`, 'Failed to start investigation', { timeoutMs: 60_000 });
}

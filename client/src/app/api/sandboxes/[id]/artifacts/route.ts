import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// POST: JSON paste (log/text/link) or multipart file upload; the proxy streams multipart.
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return forwardRequest(request, 'POST', `/api/sandboxes/${encodeURIComponent(id)}/artifacts`, 'Failed to add artifact', { timeoutMs: 60_000 });
}

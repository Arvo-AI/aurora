import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return forwardRequest(request, 'PUT', `/api/settings/sub-agents/${id}`, `settings/sub-agents/${id}`);
}

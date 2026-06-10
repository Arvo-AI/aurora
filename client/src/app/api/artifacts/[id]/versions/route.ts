import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return forwardRequest(request, 'GET', `/api/artifacts/${id}/versions`, 'Failed to fetch artifact versions');
}

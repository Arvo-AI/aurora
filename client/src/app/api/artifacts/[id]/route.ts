import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return forwardRequest(request, 'GET', `/api/artifacts/${id}`, 'Failed to fetch artifact');
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return forwardRequest(request, 'PATCH', `/api/artifacts/${id}`, 'Failed to update artifact');
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return forwardRequest(request, 'DELETE', `/api/artifacts/${id}`, 'Failed to delete artifact');
}

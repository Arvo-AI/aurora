import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

type Ctx = { params: Promise<{ id: string; artifactId: string }> };
const path = async ({ params }: Ctx) => {
  const { id, artifactId } = await params;
  return `/api/sandboxes/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifactId)}`;
};

export async function GET(request: NextRequest, ctx: Ctx) {
  return forwardRequest(request, 'GET', await path(ctx), 'Failed to fetch artifact');
}

export async function DELETE(request: NextRequest, ctx: Ctx) {
  return forwardRequest(request, 'DELETE', await path(ctx), 'Failed to delete artifact');
}

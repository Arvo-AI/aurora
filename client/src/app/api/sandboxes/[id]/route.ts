import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

type Ctx = { params: Promise<{ id: string }> };
const path = async ({ params }: Ctx) => `/api/sandboxes/${encodeURIComponent((await params).id)}`;

export async function GET(request: NextRequest, ctx: Ctx) {
  return forwardRequest(request, 'GET', await path(ctx), 'Failed to fetch sandbox');
}

export async function PATCH(request: NextRequest, ctx: Ctx) {
  return forwardRequest(request, 'PATCH', await path(ctx), 'Failed to update sandbox');
}

export async function DELETE(request: NextRequest, ctx: Ctx) {
  return forwardRequest(request, 'DELETE', await path(ctx), 'Failed to delete sandbox');
}

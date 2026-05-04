import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return forwardRequest(request, 'GET', `/api/actions/${id}`, 'action-detail');
}

export async function PUT(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return forwardRequest(request, 'PUT', `/api/actions/${id}`, 'action-update');
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return forwardRequest(request, 'DELETE', `/api/actions/${id}`, 'action-delete');
}

import { NextRequest, NextResponse } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d+$/.test(id)) {
    return NextResponse.json({ error: 'Invalid event id' }, { status: 400 });
  }
  return forwardRequest(request, 'GET', `/pipeline/events/${id}`, 'pipeline-event');
}

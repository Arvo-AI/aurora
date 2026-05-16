import { NextRequest, NextResponse } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const AGENT_ID_RE = /^[a-zA-Z0-9_-]{1,64}$/;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; agentId: string }> }
) {
  const { id, agentId } = await params;
  if (!UUID_RE.test(id)) {
    return NextResponse.json({ error: 'Invalid incident id' }, { status: 400 });
  }
  if (!AGENT_ID_RE.test(agentId)) {
    return NextResponse.json({ error: 'Invalid agent id' }, { status: 400 });
  }

  return forwardRequest(
    request,
    'GET',
    `/api/incidents/${id}/findings/${agentId}`,
    'incidents/[id]/findings/[agentId]',
  );
}

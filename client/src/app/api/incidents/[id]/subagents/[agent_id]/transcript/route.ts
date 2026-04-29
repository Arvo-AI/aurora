import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; agent_id: string }> },
) {
  const { id, agent_id } = await params;
  return forwardRequest(
    request,
    'GET',
    `/api/incidents/${id}/subagents/${agent_id}/transcript`,
    `incidents/${id}/subagents/${agent_id}/transcript`,
  );
}

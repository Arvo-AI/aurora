import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; versionId: string }> }
) {
  const { id, versionId } = await params;
  return forwardRequest(
    request,
    'GET',
    `/api/incidents/${id}/postmortem/versions/${versionId}`,
    'Failed to get postmortem version',
  );
}

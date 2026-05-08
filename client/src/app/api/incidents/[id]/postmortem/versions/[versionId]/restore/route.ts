import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string; versionId: string } }
) {
  const { id, versionId } = await params;
  return forwardRequest(
    request,
    'POST',
    `/api/incidents/${id}/postmortem/versions/${versionId}/restore`,
    'Failed to restore postmortem version',
  );
}

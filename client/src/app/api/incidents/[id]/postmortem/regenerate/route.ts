import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
  const { id } = await params;
  return forwardRequest(
    request,
    'POST',
    `/api/incidents/${id}/postmortem/regenerate`,
    'Failed to regenerate postmortem',
  );
}

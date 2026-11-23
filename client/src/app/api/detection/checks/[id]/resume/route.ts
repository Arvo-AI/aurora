import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return forwardRequest(
    request, 'POST', `/api/detection/checks/${encodeURIComponent(id)}/resume`, 'detection-checks-resume',
  );
}

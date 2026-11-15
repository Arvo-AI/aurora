import { NextRequest, NextResponse } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

// Poll a preview review. The job id is validated before it is placed in a backend path.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;
  if (!UUID_RE.test(jobId)) {
    return NextResponse.json({ error: { code: 'invalid_input', message: 'Invalid job id.' } }, { status: 400 });
  }
  return forwardRequest(request, 'GET', `/change-gating/try/${jobId}`, 'change-gating-try-status');
}

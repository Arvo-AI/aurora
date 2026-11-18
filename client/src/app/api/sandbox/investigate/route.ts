import { NextRequest, NextResponse } from 'next/server';
import { forwardPublicRequest } from '@/lib/backend-proxy';
import { readSandboxRequest, SandboxRequestError } from '@/lib/sandbox-request';

// Allow the bounded upload window plus the backend proxy deadline.
export const maxDuration = 120;

export async function POST(request: NextRequest) {
  try {
    const body = await readSandboxRequest(request);
    const boundedRequest = new NextRequest(request.url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
    });
    const response = await forwardPublicRequest(boundedRequest, 'POST', '/sandbox/investigate', 'sample investigation', {
      timeoutMs: 95_000,
    });
    response.headers.set('Cache-Control', 'no-store');
    return response;
  } catch (error) {
    return NextResponse.json({ error: error instanceof SandboxRequestError ? error.message : 'The request could not be read. Please retry.' }, {
      status: error instanceof SandboxRequestError ? error.status : 400,
      headers: { 'Cache-Control': 'no-store' },
    });
  }
}

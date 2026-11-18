import { NextRequest, NextResponse } from 'next/server';
import { forwardPublicRequest } from '@/lib/backend-proxy';
import { readSandboxRequest, SandboxRequestError } from '@/lib/sandbox-request';

export const maxDuration = 90;

export async function POST(request: NextRequest) {
  try {
    const body = await readSandboxRequest(request, 10_000, 'challenge');
    const boundedRequest = new NextRequest(request.url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
    });
    const response = await forwardPublicRequest(boundedRequest, 'POST', '/sandbox/challenge', 'diagnosis challenge', { timeoutMs: 65_000 });
    response.headers.set('Cache-Control', 'no-store');
    return response;
  } catch (error) {
    return NextResponse.json({ error: error instanceof SandboxRequestError ? error.message : 'The request could not be read. Please retry.' }, {
      status: error instanceof SandboxRequestError ? error.status : 400,
      headers: { 'Cache-Control': 'no-store' },
    });
  }
}

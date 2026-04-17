import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 30000;

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json(
        { error: 'BACKEND_URL is not configured' },
        { status: 500 },
      );
    }

    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { id } = await params;

    if (!id) {
      return NextResponse.json({ error: 'Missing incident id' }, { status: 400 });
    }

    let body = '';
    try {
      body = await request.text();
    } catch {
      body = '';
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(
        `${API_BASE_URL}/api/incidents/${encodeURIComponent(id)}/postmortem/export/notion`,
        {
          method: 'POST',
          headers: {
            ...authHeaders,
            'Content-Type': 'application/json',
          },
          body: body || JSON.stringify({}),
          credentials: 'include',
          cache: 'no-store',
          signal: controller.signal,
        },
      );
      const text = await response.text();
      return new NextResponse(text, {
        status: response.status,
        headers: {
          'Content-Type': response.headers.get('content-type') || 'application/json',
        },
      });
    } finally {
      // Clear only after body read; fetch can return with headers flushed but
      // body still streaming, so the abort signal must stay live through text().
      clearTimeout(timeoutId);
    }
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.error('[api/incidents/[id]/postmortem/export/notion] Request timeout');
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error(
      '[api/incidents/[id]/postmortem/export/notion] Error:',
      error instanceof Error ? error.message : 'Unknown error',
    );
    return NextResponse.json(
      { error: 'Failed to export postmortem to Notion' },
      { status: 500 },
    );
  }
}

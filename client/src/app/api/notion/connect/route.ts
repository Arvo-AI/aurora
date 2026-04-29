import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isSafeFetchTimeout, safeFetch } from '@/lib/safe-fetch';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 15_000;

export async function POST(request: NextRequest) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL is not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }
    const { headers: authHeaders } = authResult;

    let body = '';
    try {
      body = await request.text();
    } catch {
      body = '';
    }

    const response = await safeFetch(`${API_BASE_URL}/notion/connect`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: body || JSON.stringify({}),
      credentials: 'include',
      timeoutMs: FETCH_TIMEOUT_MS,
    });

    const text = await response.text();
    return new NextResponse(text, {
      status: response.status,
      headers: {
        'Content-Type': response.headers.get('content-type') || 'application/json',
      },
    });
  } catch (error) {
    if (isSafeFetchTimeout(error)) {
      console.error('[api/notion/connect] Request timeout');
      return NextResponse.json({ error: 'Connection timeout' }, { status: 504 });
    }
    console.error('[api/notion/connect] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to connect Notion' }, { status: 500 });
  }
}

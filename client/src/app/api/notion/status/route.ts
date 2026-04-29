import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isSafeFetchTimeout, safeFetch } from '@/lib/safe-fetch';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 15_000;

export async function GET() {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL is not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }
    const { headers: authHeaders } = authResult;

    const response = await safeFetch(`${API_BASE_URL}/notion/status`, {
      method: 'GET',
      headers: authHeaders,
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
      console.error('[api/notion/status] Request timeout');
      return NextResponse.json({ error: 'Status request timeout' }, { status: 504 });
    }
    console.error('[api/notion/status] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to fetch Notion status' }, { status: 500 });
  }
}

import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isSafeFetchTimeout, safeFetch } from '@/lib/safe-fetch';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 12_000;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;

    const response = await safeFetch(`${API_BASE_URL}/atlassian/status`, {
      headers: { ...authHeaders, 'Accept': 'application/json' },
      credentials: 'include',
      cache: 'no-store',
      timeoutMs: FETCH_TIMEOUT_MS,
    });
    if (!response.ok) {
      const text = await response.text();
      console.error('[api/atlassian/status] Backend error:', text);
      return NextResponse.json({ error: 'Failed to get status' }, { status: response.status });
    }
    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (isSafeFetchTimeout(error)) {
      return NextResponse.json({ error: 'Status check timeout' }, { status: 504 });
    }
    console.error('[api/atlassian/status] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to get Atlassian status' }, { status: 500 });
  }
}

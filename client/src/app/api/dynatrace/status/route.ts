import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isSafeFetchTimeout, safeFetch } from '@/lib/safe-fetch';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await safeFetch(`${API_BASE_URL}/dynatrace/status`, {
      method: 'GET',
      headers: authResult.headers,
      credentials: 'include',
      timeoutMs: 15_000,
    });

    if (!response.ok) {
      console.error('[api/dynatrace/status] Backend error:', await response.text());
      return NextResponse.json({ error: 'Failed to get Dynatrace status' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    if (isSafeFetchTimeout(error)) {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/dynatrace/status] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to get Dynatrace status' }, { status: 500 });
  }
}

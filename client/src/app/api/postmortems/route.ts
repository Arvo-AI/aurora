import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isSafeFetchTimeout, safeFetch } from '@/lib/safe-fetch';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(_request: NextRequest) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json(
        { error: 'BACKEND_URL not configured' },
        { status: 500 }
      );
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }
    const { headers: authHeaders } = authResult;

    const response = await safeFetch(`${API_BASE_URL}/api/postmortems`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
      timeoutMs: 30_000,
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json(
        { error: text || 'Failed to get postmortems' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (isSafeFetchTimeout(error)) {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/postmortems] GET Error:', error);
    return NextResponse.json({ error: 'Failed to get postmortems' }, { status: 500 });
  }
}

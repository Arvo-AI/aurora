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

    const response = await safeFetch(`${API_BASE_URL}/sharepoint/status`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      timeoutMs: FETCH_TIMEOUT_MS,
    });

    if (!response.ok) {
      console.error('[api/sharepoint/status] Backend error: status=%d', response.status);
      let errorMessage = 'Failed to fetch SharePoint status';
      try {
        const errorData = await response.json();
        if (errorData?.error) {
          errorMessage = errorData.error;
        }
      } catch {
        // response not JSON, use default message
      }
      return NextResponse.json({ error: errorMessage }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (isSafeFetchTimeout(error)) {
      console.error('[api/sharepoint/status] Request timeout');
      return NextResponse.json({ error: 'Status request timeout' }, { status: 504 });
    }
    console.error('[api/sharepoint/status] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to fetch SharePoint status' }, { status: 500 });
  }
}

import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 12000;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/atlassian/status`, {
        headers: { ...authHeaders, 'Accept': 'application/json' },
        credentials: 'include',
        cache: 'no-store',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }
    if (!response.ok) {
      const text = await response.text();
      console.error('[api/atlassian/status] Backend error:', text);
      return NextResponse.json({ error: 'Failed to get status' }, { status: response.status });
    }
    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Status check timeout' }, { status: 504 });
    }
    console.error('[api/atlassian/status] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to get Atlassian status' }, { status: 500 });
  }
}

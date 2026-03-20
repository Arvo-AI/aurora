import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    if (!API_BASE_URL) {
      console.error('[api/rootly/status] BACKEND_URL environment variable is not configured');
      return NextResponse.json({ error: 'Server configuration error' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);

    try {
      const response = await fetch(`${API_BASE_URL}/rootly/status`, {
        method: 'GET',
        headers: authResult.headers,
        credentials: 'include',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        console.error('[api/rootly/status] Backend error:', await response.text());
        return NextResponse.json({ error: 'Failed to get Rootly status' }, { status: response.status });
      }
      return NextResponse.json(await response.json());
    } finally {
      clearTimeout(timeoutId);
    }
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/rootly/status] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to get Rootly status' }, { status: 500 });
  }
}

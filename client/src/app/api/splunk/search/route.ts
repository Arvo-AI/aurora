import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 120000; // 2 minutes for search

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const payload = await request.json();

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/splunk/search`, {
        method: 'POST',
        headers: {
          ...authHeaders,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      const text = await response.text();
      let errorMsg = 'Search failed';
      try {
        const parsed = JSON.parse(text);
        errorMsg = parsed.error || errorMsg;
      } catch {
        errorMsg = text || errorMsg;
      }
      console.error('[api/splunk/search] Backend error:', text);
      return NextResponse.json({ error: errorMsg }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.error('[api/splunk/search] Request timeout');
      return NextResponse.json({ error: 'Search timed out' }, { status: 504 });
    }
    console.error('[api/splunk/search] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Search failed' }, { status: 500 });
  }
}

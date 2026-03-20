import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  try {
    if (!API_BASE_URL) {
      console.error('[api/rootly/connect] BACKEND_URL environment variable is not configured');
      return NextResponse.json({ error: 'Server configuration error' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const payload = await request.json();
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);

    try {
      const response = await fetch(`${API_BASE_URL}/rootly/connect`, {
        method: 'POST',
        headers: { ...authResult.headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        const text = await response.text();
        console.error('[api/rootly/connect] Backend error:', text);
        let errorMessage = 'Failed to connect to Rootly';
        try {
          const parsed = JSON.parse(text);
          errorMessage = parsed.error || errorMessage;
        } catch { /* use default */ }
        return NextResponse.json({ error: errorMessage }, { status: response.status });
      }
      return NextResponse.json(await response.json());
    } finally {
      clearTimeout(timeoutId);
    }
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Connection timeout' }, { status: 504 });
    }
    console.error('[api/rootly/connect] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to connect to Rootly' }, { status: 500 });
  }
}

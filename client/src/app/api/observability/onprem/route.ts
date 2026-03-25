import { NextResponse, NextRequest } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 20000;

export async function POST(request: NextRequest) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const body = await request.json();

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/api/observability/onprem`, {
        method: 'POST',
        headers: {
          ...authResult.headers,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      const text = await response.text();
      console.error('[api/observability/onprem] Backend error:', text);
      return NextResponse.json({ error: 'Failed to register resource' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/observability/onprem] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to register on-prem resource' }, { status: 500 });
  }
}

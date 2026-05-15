import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 15000;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL is not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { id } = await params;

    if (!id) {
      return NextResponse.json({ error: 'Missing database id' }, { status: 400 });
    }

    const search = request.nextUrl.searchParams.toString();
    const encodedId = encodeURIComponent(id);
    const target = search
      ? `${API_BASE_URL}/notion/databases/${encodedId}?${search}`
      : `${API_BASE_URL}/notion/databases/${encodedId}`;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(target, {
        method: 'GET',
        headers: authHeaders,
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    const text = await response.text();
    return new NextResponse(text, {
      status: response.status,
      headers: {
        'Content-Type': response.headers.get('content-type') || 'application/json',
      },
    });
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.error('[api/notion/databases/:id] Request timeout');
      return NextResponse.json({ error: 'Database request timeout' }, { status: 504 });
    }
    console.error('[api/notion/databases/:id] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to fetch Notion database' }, { status: 500 });
  }
}

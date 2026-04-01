import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

const SSE_PATHS = new Set(['health/stream']);

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  try {
    if (!API_BASE_URL) return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { path } = await params;
    const subpath = path.join('/');
    const searchParams = request.nextUrl.searchParams.toString();
    const backendUrl = `${API_BASE_URL}/api/monitor/${subpath}${searchParams ? `?${searchParams}` : ''}`;

    const response = await fetch(backendUrl, {
      method: 'GET',
      headers: authResult.headers,
      credentials: 'include',
      ...(SSE_PATHS.has(subpath) ? {} : { cache: 'no-store' as const }),
    });

    if (!response.ok) {
      const text = await response.text();
      return SSE_PATHS.has(subpath)
        ? new Response(text || 'Stream error', { status: response.status })
        : NextResponse.json({ error: text || 'Backend error' }, { status: response.status });
    }

    if (SSE_PATHS.has(subpath)) {
      return new Response(response.body, {
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive' },
      });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error(`[api/monitor] Error:`, error);
    return NextResponse.json({ error: 'Request failed' }, { status: 500 });
  }
}

import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 15000;

async function proxyToBackend(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { path } = await params;
    const subPath = path.join('/');
    const { headers: authHeaders } = authResult;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    const fetchOptions: RequestInit = {
      method: request.method,
      headers: { ...authHeaders, 'Content-Type': 'application/json' },
      credentials: 'include',
      signal: controller.signal,
    };

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      const body = await request.text();
      if (body) fetchOptions.body = body;
    }

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/coroot/${subPath}`, fetchOptions);
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      const text = await response.text();
      console.error(`[api/coroot/${subPath}] Backend error:`, text);
      let errorMessage = 'Backend request failed';
      try {
        const parsed = JSON.parse(text);
        if (parsed?.error) errorMessage = parsed.error;
      } catch {
        // not JSON — keep default message
      }
      return NextResponse.json(
        { error: errorMessage },
        { status: response.status },
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Connection timeout' }, { status: 504 });
    }
    console.error('[api/coroot] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export const GET = proxyToBackend;
export const POST = proxyToBackend;
export const PUT = proxyToBackend;
export const DELETE = proxyToBackend;

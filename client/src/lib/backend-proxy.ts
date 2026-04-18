import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

function buildUrl(backendPath: string, qs?: string): string {
  return qs ? `${API_BASE_URL}${backendPath}?${qs}` : `${API_BASE_URL}${backendPath}`;
}

export async function forwardRequest(
  request: NextRequest,
  method: string,
  backendPath: string,
  errorLabel: string,
  options: { timeoutMs?: number; passBody?: boolean } = {},
): Promise<NextResponse> {
  const { timeoutMs = 10_000, passBody = method !== 'GET' } = options;

  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;

    const { searchParams } = new URL(request.url);
    const qs = searchParams.toString();
    const url = buildUrl(backendPath, qs);

    const headers: Record<string, string> = { ...authHeaders };

    let body: BodyInit | undefined;
    if (passBody) {
      const ct = request.headers.get('content-type') || '';
      if (ct.includes('application/json')) {
        headers['Content-Type'] = 'application/json';
        body = await request.text();
      } else if (ct.includes('multipart/form-data')) {
        body = await request.arrayBuffer() as unknown as BodyInit;
        headers['Content-Type'] = ct;
      } else if (ct.includes('application/x-www-form-urlencoded')) {
        headers['Content-Type'] = ct;
        body = await request.text();
      } else {
        headers['Content-Type'] = 'application/json';
        try {
          body = await request.text();
        } catch {
          // no body
        }
      }
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body,
        credentials: 'include',
        cache: 'no-store',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
    } catch (fetchErr: unknown) {
      clearTimeout(timeoutId);
      if (fetchErr instanceof Error && fetchErr.name === 'AbortError') {
        return NextResponse.json(
          { error: `Request timeout for ${errorLabel}` },
          { status: 504 },
        );
      }
      throw fetchErr;
    }

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json(
        { error: text || `Failed to fetch ${errorLabel}` },
        { status: response.status },
      );
    }

    const ct = response.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      const data = await response.json();
      return NextResponse.json(data);
    }

    const text = await response.text();
    return new NextResponse(text, {
      status: response.status,
      headers: { 'Content-Type': ct || 'text/plain' },
    });
  } catch (error) {
    console.error(`[api/${errorLabel}] Error:`, error);
    return NextResponse.json(
      { error: `Failed to load ${errorLabel}` },
      { status: 500 },
    );
  }
}

export { API_BASE_URL };

import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { env } from '@/lib/server-env';

const METHODS_WITHOUT_BODY = new Set(['GET', 'HEAD', 'OPTIONS']);

function buildUrl(backendPath: string, qs?: string): string {
  return qs ? `${env.BACKEND_URL}${backendPath}?${qs}` : `${env.BACKEND_URL}${backendPath}`;
}

export async function forwardRequest(
  request: NextRequest,
  method: string,
  backendPath: string,
  errorLabel: string,
  options: { timeoutMs?: number; passBody?: boolean } = {},
): Promise<NextResponse> {
  const { timeoutMs = 30_000, passBody = !METHODS_WITHOUT_BODY.has(method.toUpperCase()) } = options;

  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;

    const { searchParams } = new URL(request.url);
    const qs = searchParams.toString();
    const url = buildUrl(backendPath, qs);

    const headers: Record<string, string> = { ...authHeaders };

    if (env.INTERNAL_API_SECRET) {
      headers['X-Internal-Secret'] = env.INTERNAL_API_SECRET;
    }

    let body: BodyInit | undefined;
    if (passBody) {
      const ct = request.headers.get('content-type') || '';
      if (ct.includes('multipart/form-data')) {
        body = request.body as unknown as BodyInit;
        headers['Content-Type'] = ct;
      } else if (ct.includes('application/json')) {
        headers['Content-Type'] = 'application/json';
        body = await request.text();
      } else if (ct.includes('application/x-www-form-urlencoded')) {
        headers['Content-Type'] = ct;
        body = await request.text();
      } else {
        try {
          const text = await request.text();
          if (text.length > 0) {
            headers['Content-Type'] = 'application/json';
            body = text;
          }
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

    // 204/304 have no body — reading it would throw
    if (response.status === 204 || response.status === 304) {
      return new NextResponse(null, { status: response.status });
    }

    if (ct.includes('application/json')) {
      const data = await response.json();
      return NextResponse.json(data, { status: response.status });
    }

    // Redact non-JSON responses (e.g. werkzeug debug pages) to prevent info leakage
    if (ct.includes('text/html')) {
      return NextResponse.json(
        { error: `Unexpected HTML response from ${errorLabel}` },
        { status: 502 },
      );
    }

    const text = await response.text();
    return new NextResponse(text, {
      status: response.status,
      headers: { 'Content-Type': ct || 'text/plain' },
    });
  } catch (error) {
    const safeError = error instanceof Error ? { message: error.message, name: error.name } : {};
    console.error(`[api/${errorLabel}] Error:`, safeError);
    return NextResponse.json(
      { error: `Failed to load ${errorLabel}` },
      { status: 500 },
    );
  }
}

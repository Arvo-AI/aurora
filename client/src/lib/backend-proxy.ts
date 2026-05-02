import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { env } from '@/lib/server-env';
import { safeFetch, isSafeFetchTimeout } from '@/lib/safe-fetch';

const METHODS_WITHOUT_BODY = new Set(['GET', 'HEAD', 'OPTIONS']);

function buildUrl(backendPath: string, qs?: string): string {
  return qs ? `${env.BACKEND_URL}${backendPath}?${qs}` : `${env.BACKEND_URL}${backendPath}`;
}

// Extract and forward the request body; multipart is streamed, everything else buffered as text.
async function prepareBody(
  request: NextRequest,
  headers: Record<string, string>,
): Promise<{ body: BodyInit | undefined; useDuplex: boolean }> {
  const ct = request.headers.get('content-type') || '';

  if (ct.includes('multipart/form-data')) {
    headers['Content-Type'] = ct;
    return { body: request.body as unknown as BodyInit, useDuplex: true };
  }

  if (ct.includes('application/json')) {
    headers['Content-Type'] = 'application/json';
    return { body: await request.text(), useDuplex: false };
  }

  if (ct.includes('application/x-www-form-urlencoded')) {
    headers['Content-Type'] = ct;
    return { body: await request.text(), useDuplex: false };
  }

  try {
    const text = await request.text();
    if (text.length > 0) {
      if (ct) headers['Content-Type'] = ct;
      return { body: text, useDuplex: false };
    }
  } catch {
    // no body
  }

  return { body: undefined, useDuplex: false };
}

// Turn a successful backend response into a NextResponse.
function formatSuccessResponse(response: Response, errorLabel: string): Promise<NextResponse> | NextResponse {
  // 204/304 have no body — reading it would throw
  if (response.status === 204 || response.status === 304) {
    return new NextResponse(null, { status: response.status });
  }

  const ct = response.headers.get('content-type') || '';

  if (ct.includes('application/json')) {
    return response.json().then((data) => NextResponse.json(data, { status: response.status }));
  }

  // Redact HTML (e.g. werkzeug debug pages) to prevent info leakage
  if (ct.includes('text/html')) {
    return NextResponse.json(
      { error: `Unexpected HTML response from ${errorLabel}` },
      { status: 502 },
    );
  }

  // Fall back to plain text
  return response.text().then((text) =>
    new NextResponse(text, {
      status: response.status,
      headers: { 'Content-Type': ct || 'text/plain' },
    }),
  );
}

// Proxy a Next.js API-route request to the Python backend with auth, timeout, and error normalisation.
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
    const url = buildUrl(backendPath, searchParams.toString());

    const headers: Record<string, string> = { ...authHeaders };
    if (env.INTERNAL_API_SECRET) {
      headers['X-Internal-Secret'] = env.INTERNAL_API_SECRET;
    }
    // Bun's keepalive pool can wedge the second chat POST on a stale socket.
    // Scoped to /api/chat/* so non-chat writes keep their connection reuse.
    if (
      method.toUpperCase() !== 'GET' &&
      backendPath.startsWith('/api/chat/')
    ) {
      headers['Connection'] = 'close';
    }

    let body: BodyInit | undefined;
    let useDuplex = false;
    if (passBody) {
      ({ body, useDuplex } = await prepareBody(request, headers));
    }

    // safeFetch (Promise.race) — Bun stale-socket hang; AbortController alone
    // is unreliable here. See safe-fetch.ts.
    let response: Response;
    try {
      response = await safeFetch(url, {
        method,
        headers,
        body,
        credentials: 'include',
        cache: 'no-store',
        timeoutMs,
        ...(useDuplex ? { duplex: 'half' as const } : {}),
      } as RequestInit & { timeoutMs?: number });
    } catch (fetchErr: unknown) {
      if (isSafeFetchTimeout(fetchErr)) {
        return NextResponse.json({ error: `Request timeout for ${errorLabel}` }, { status: 504 });
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

    return await formatSuccessResponse(response, errorLabel);
  } catch (error) {
    const safeError = error instanceof Error ? { message: error.message, name: error.name } : {};
    console.error(`[api/${errorLabel}] Error:`, safeError);
    return NextResponse.json({ error: `Failed to load ${errorLabel}` }, { status: 500 });
  }
}

/**
 * Forward an authenticated GET request to a backend API path,
 * passing through query-string parameters and auth headers.
 */
export async function forwardAuthenticatedGet(
  request: NextRequest,
  backendPath: string,
  errorLabel: string,
): Promise<NextResponse> {
  return forwardRequest(request, 'GET', backendPath, errorLabel);
}

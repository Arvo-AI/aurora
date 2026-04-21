import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { env } from '@/lib/server-env';

const METHODS_WITHOUT_BODY = new Set(['GET', 'HEAD', 'OPTIONS']);

function buildUrl(backendPath: string, qs?: string): string {
  return qs ? `${env.BACKEND_URL}${backendPath}?${qs}` : `${env.BACKEND_URL}${backendPath}`;
}

/**
 * Extract and forward the request body, setting the appropriate Content-Type header.
 * Multipart requests are streamed (duplex); everything else is buffered as text.
 */
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

/**
 * Convert a successful backend response into a NextResponse.
 * 204/304 have no body — reading it would throw, so return early.
 * HTML is redacted (e.g. werkzeug debug pages) to prevent info leakage.
 */
function formatSuccessResponse(response: Response, errorLabel: string): Promise<NextResponse> | NextResponse {
  if (response.status === 204 || response.status === 304) {
    return new NextResponse(null, { status: response.status });
  }

  const ct = response.headers.get('content-type') || '';

  if (ct.includes('application/json')) {
    return response.json().then((data) => NextResponse.json(data, { status: response.status }));
  }

  if (ct.includes('text/html')) {
    return NextResponse.json(
      { error: `Unexpected HTML response from ${errorLabel}` },
      { status: 502 },
    );
  }

  return response.text().then((text) =>
    new NextResponse(text, {
      status: response.status,
      headers: { 'Content-Type': ct || 'text/plain' },
    }),
  );
}

/**
 * Proxy a Next.js API-route request to the Python backend.
 * Handles auth, timeout, body forwarding, and error normalisation.
 */
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

    let body: BodyInit | undefined;
    let useDuplex = false;
    if (passBody) {
      ({ body, useDuplex } = await prepareBody(request, headers));
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
        ...(useDuplex ? { duplex: 'half' as const } : {}),
      } as RequestInit);
      clearTimeout(timeoutId);
    } catch (fetchErr: unknown) {
      clearTimeout(timeoutId);
      if (fetchErr instanceof Error && fetchErr.name === 'AbortError') {
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

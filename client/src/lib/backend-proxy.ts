import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const DEFAULT_TIMEOUT_MS = 15_000;

export async function forwardAuthenticatedRequest(
  backendPath: string,
  errorLabel: string,
  options: {
    method?: string;
    request?: NextRequest | Request;
    body?: unknown;
    timeoutMs?: number;
  } = {},
): Promise<NextResponse> {
  const { method = 'GET', request, body, timeoutMs = DEFAULT_TIMEOUT_MS } = options;

  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;

    let qs = '';
    if (request && method === 'GET') {
      const { searchParams } = new URL(request.url);
      qs = searchParams.toString();
    }

    const url = qs
      ? `${API_BASE_URL}${backendPath}?${qs}`
      : `${API_BASE_URL}${backendPath}`;

    const fetchHeaders: Record<string, string> = { ...authHeaders };
    if (body !== undefined) {
      fetchHeaders['Content-Type'] = 'application/json';
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers: fetchHeaders,
        credentials: 'include',
        cache: 'no-store',
        signal: controller.signal,
        ...(body !== undefined && { body: JSON.stringify(body) }),
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json(
        { error: text || `Failed to fetch ${errorLabel}` },
        { status: response.status },
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json(
        { error: `Request timeout for ${errorLabel}` },
        { status: 504 },
      );
    }
    console.error(`[api/${errorLabel}] Error:`, error);
    return NextResponse.json(
      { error: `Failed to load ${errorLabel}` },
      { status: 500 },
    );
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
  return forwardAuthenticatedRequest(backendPath, errorLabel, {
    method: 'GET',
    request,
  });
}

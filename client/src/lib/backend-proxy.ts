import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

/**
 * Forward an authenticated GET request to a backend API path,
 * passing through query-string parameters and auth headers.
 */
export async function forwardAuthenticatedGet(
  request: NextRequest,
  backendPath: string,
  errorLabel: string,
): Promise<NextResponse> {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;

    const { searchParams } = new URL(request.url);
    const qs = searchParams.toString();
    const url = qs
      ? `${API_BASE_URL}${backendPath}?${qs}`
      : `${API_BASE_URL}${backendPath}`;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10_000);

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'GET',
        headers: authHeaders,
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

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error(`[api/${errorLabel}] Error:`, error);
    return NextResponse.json(
      { error: `Failed to load ${errorLabel}` },
      { status: 500 },
    );
  }
}

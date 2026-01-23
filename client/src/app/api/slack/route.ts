import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

// Error messages for each method
const ERROR_MESSAGES = {
  GET: 'Failed to load Slack status',
  POST: 'Failed to initiate Slack OAuth',
  DELETE: 'Failed to disconnect Slack',
} as const;

// Helper function to proxy requests to backend
async function proxyToBackend(
  method: 'GET' | 'POST' | 'DELETE',
  request?: NextRequest
): Promise<NextResponse> {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    // Prepare request options
    const fetchOptions: RequestInit = {
      method,
      headers: {
        ...authHeaders,
        ...(method === 'POST' && { 'Content-Type': 'application/json' }),
      },
      credentials: 'include',
      ...(method === 'GET' && { cache: 'no-store' }),
    };

    // Add body for POST requests
    if (method === 'POST' && request) {
      const payload = await request.json().catch(() => ({}));
      fetchOptions.body = JSON.stringify(payload);
    }

    const response = await fetch(`${API_BASE_URL}/slack`, fetchOptions);

    if (!response.ok) {
      const text = await response.text();
      try {
        const jsonError = JSON.parse(text);
        return NextResponse.json(jsonError, { status: response.status });
      } catch {
        return NextResponse.json(
          { error: text || ERROR_MESSAGES[method] },
          { status: response.status }
        );
      }
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error(`[api/slack] ${method} Error:`, error);
    return NextResponse.json(
      { error: ERROR_MESSAGES[method] },
      { status: 500 }
    );
  }
}

export async function GET() {
  return proxyToBackend('GET');
}

export async function POST(request: NextRequest) {
  return proxyToBackend('POST', request);
}

export async function DELETE() {
  return proxyToBackend('DELETE');
}


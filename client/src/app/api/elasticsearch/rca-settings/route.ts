import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 15000;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/elasticsearch/rca-settings`, {
        method: 'GET',
        headers: authHeaders,
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      return NextResponse.json({ error: 'Failed to get RCA settings' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    return NextResponse.json({ error: 'Failed to get RCA settings' }, { status: 500 });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const payload = await request.json();

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/elasticsearch/rca-settings`, {
        method: 'PUT',
        headers: {
          ...authHeaders,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      return NextResponse.json({ error: 'Failed to update RCA settings' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    return NextResponse.json({ error: 'Failed to update RCA settings' }, { status: 500 });
  }
}

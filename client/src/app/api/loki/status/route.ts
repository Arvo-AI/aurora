import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json(
        { error: 'BACKEND_URL not configured' },
        { status: 500 }
      );
    }

    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    const response = await fetch(`${API_BASE_URL}/loki/status`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error('[api/loki/status] Backend error:', response.status, errorText);
      return NextResponse.json(
        { error: 'Failed to fetch Loki status' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/loki/status] Error:', error);
    return NextResponse.json({ error: 'Failed to load Loki status' }, { status: 500 });
  }
}

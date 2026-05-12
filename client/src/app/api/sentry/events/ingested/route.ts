import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const search = request.nextUrl.search || '';

    const response = await fetch(`${API_BASE_URL}/sentry/events/ingested${search}`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch Sentry events' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/sentry/events/ingested] Error:', error);
    return NextResponse.json({ error: 'Failed to load Sentry events' }, { status: 500 });
  }
}

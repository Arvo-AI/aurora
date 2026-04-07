import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    if (!API_BASE_URL) return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const params = request.nextUrl.searchParams.toString();
    const url = `${API_BASE_URL}/api/monitor/fleet${params ? `?${params}` : ''}`;
    const response = await fetch(url, { method: 'GET', headers: authHeaders, credentials: 'include', cache: 'no-store' });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch fleet data' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/monitor/fleet] Error:', error);
    return NextResponse.json({ error: 'Failed to load fleet data' }, { status: 500 });
  }
}

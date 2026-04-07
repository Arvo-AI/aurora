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
    const url = `${API_BASE_URL}/api/monitor/tools/stats${params ? `?${params}` : ''}`;
    const response = await fetch(url, { method: 'GET', headers: authHeaders, credentials: 'include', cache: 'no-store' });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch tool stats' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/monitor/tools/stats] Error:', error);
    return NextResponse.json({ error: 'Failed to load tool stats' }, { status: 500 });
  }
}

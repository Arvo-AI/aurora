import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ incidentId: string }> }
) {
  try {
    if (!API_BASE_URL) return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const { incidentId } = await params;
    const url = `${API_BASE_URL}/api/monitor/fleet/${encodeURIComponent(incidentId)}/activity`;
    const response = await fetch(url, { method: 'GET', headers: authHeaders, credentials: 'include', cache: 'no-store' });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch activity' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/monitor/fleet/activity] Error:', error);
    return NextResponse.json({ error: 'Failed to load activity' }, { status: 500 });
  }
}

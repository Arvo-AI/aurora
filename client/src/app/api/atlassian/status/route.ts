import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const response = await fetch(`${API_BASE_URL}/atlassian/status`, {
      headers: { ...authHeaders, 'Accept': 'application/json' },
      credentials: 'include',
      cache: 'no-store',
    });
    if (!response.ok) {
      const text = await response.text();
      console.error('[api/atlassian/status] Backend error:', text);
      return NextResponse.json({ error: 'Failed to get status' }, { status: response.status });
    }
    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/atlassian/status] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to get Atlassian status' }, { status: 500 });
  }
}

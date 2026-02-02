import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(
  request: NextRequest,
  { params }: { params: { id: string } }
) {
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
    const { id } = await params;

    const response = await fetch(`${API_BASE_URL}/api/incidents/${id}/visualization`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch visualization' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/incidents/[id]/visualization] Error:', error);
    return NextResponse.json({ error: 'Failed to load visualization' }, { status: 500 });
  }
}

import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  if (!API_BASE_URL) {
    console.error('[api/jenkins/status] BACKEND_URL not configured');
    return NextResponse.json({ error: 'Server configuration error' }, { status: 500 });
  }

  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    const response = await fetch(`${API_BASE_URL}/jenkins/status`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch Jenkins status' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/jenkins/status] Error:', error);
    return NextResponse.json({ error: 'Failed to load Jenkins status' }, { status: 500 });
  }
}

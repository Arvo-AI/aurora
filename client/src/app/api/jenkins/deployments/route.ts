import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { searchParams } = new URL(request.url);
    const limitParam = searchParams.get('limit');
    // Validate and clamp limit to 1-100
    const limit = Math.min(Math.max(parseInt(limitParam || '20', 10) || 20, 1), 100);

    const response = await fetch(`${API_BASE_URL}/jenkins/deployments?limit=${limit}`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch deployments' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/jenkins/deployments] Error:', error);
    return NextResponse.json({ error: 'Failed to load deployments' }, { status: 500 });
  }
}

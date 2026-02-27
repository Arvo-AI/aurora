import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  if (!API_BASE_URL) {
    console.error('[api/jenkins/connect] BACKEND_URL not configured');
    return NextResponse.json({ error: 'Server configuration error' }, { status: 500 });
  }

  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const payload = await request.json();

    const response = await fetch(`${API_BASE_URL}/jenkins/connect`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      credentials: 'include',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to connect Jenkins' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/jenkins/connect] Error:', error);
    return NextResponse.json({ error: 'Failed to connect Jenkins' }, { status: 500 });
  }
}

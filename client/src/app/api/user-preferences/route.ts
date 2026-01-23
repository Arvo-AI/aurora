import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    if (!API_BASE_URL) return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { searchParams } = new URL(request.url);
    const key = searchParams.get('key');
    if (!key) return NextResponse.json({ error: 'Missing key parameter' }, { status: 400 });

    const response = await fetch(`${API_BASE_URL}/api/user-preferences?key=${encodeURIComponent(key)}`, {
      method: 'GET',
      headers: authResult.headers,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      console.error('[api/user-preferences] Backend error:', response.status, text);
      return NextResponse.json({ error: text || 'Failed to fetch preference' }, { status: response.status });
    }

    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/user-preferences] Error:', error);
    return NextResponse.json({ error: 'Failed to load preference' }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    if (!API_BASE_URL) return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const body = await request.json();
    if (!body.key) return NextResponse.json({ error: 'Missing key in request body' }, { status: 400 });

    const response = await fetch(`${API_BASE_URL}/api/user-preferences`, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const text = await response.text();
      console.error('[api/user-preferences] Backend error:', response.status, text);
      return NextResponse.json({ error: text || 'Failed to save preference' }, { status: response.status });
    }

    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/user-preferences] Error:', error);
    return NextResponse.json({ error: 'Failed to save preference' }, { status: 500 });
  }
}


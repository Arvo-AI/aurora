import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const BACKEND_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await fetch(`${BACKEND_URL}/pagerduty/oauth/login`, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      credentials: 'include',
      body: '{}',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'OAuth init failed' }, { status: response.status });
    }

    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/pagerduty/oauth/login]', error);
    return NextResponse.json({ error: 'OAuth init failed' }, { status: 500 });
  }
}


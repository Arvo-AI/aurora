import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const BACKEND_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
    }

    const response = await fetch(`${BACKEND_URL}/api/gcp/wif/connect`, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body ?? {}),
    });

    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = null; }
    }

    if (!response.ok) {
      const errorMessage =
        (payload && typeof payload === 'object' && 'error' in payload &&
          typeof (payload as { error: unknown }).error === 'string'
          ? (payload as { error: string }).error
          : null) || text || 'WIF connect failed';
      return NextResponse.json({ error: errorMessage }, { status: response.status });
    }

    return NextResponse.json(payload ?? {});
  } catch (error) {
    console.error('[api/gcp/wif/connect]', error);
    return NextResponse.json({ error: 'WIF connect failed' }, { status: 500 });
  }
}

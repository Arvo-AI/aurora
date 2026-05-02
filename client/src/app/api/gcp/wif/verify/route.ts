import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const BACKEND_URL = process.env.BACKEND_URL;

export async function POST() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await fetch(`${BACKEND_URL}/api/gcp/wif/verify`, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      credentials: 'include',
      body: '{}',
    });

    const payload = await response.json().catch(() => null);

    if (!response.ok) {
      const errorMessage =
        (payload && typeof payload === 'object' && 'error' in payload
          ? (payload as { error: string }).error
          : null) || 'WIF verify failed';
      return NextResponse.json({ error: errorMessage }, { status: response.status });
    }

    return NextResponse.json(payload ?? {});
  } catch (error) {
    console.error('[api/gcp/wif/verify]', error);
    return NextResponse.json({ error: 'WIF verify failed' }, { status: 500 });
  }
}

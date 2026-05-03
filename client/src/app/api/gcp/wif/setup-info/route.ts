import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const BACKEND_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await fetch(`${BACKEND_URL}/api/gcp/wif/setup-info`, {
      headers: authResult.headers,
      credentials: 'include',
    });

    const data = await response.json().catch(() => null);
    if (!response.ok) {
      return NextResponse.json(
        data ?? { error: 'Failed to fetch WIF setup info' },
        { status: response.status },
      );
    }

    return NextResponse.json(data ?? {});
  } catch (error) {
    console.error('[api/gcp/wif/setup-info]', error);
    return NextResponse.json({ error: 'Failed to fetch WIF setup info' }, { status: 500 });
  }
}

import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    const response = await fetch(`${API_BASE_URL}/google-chat/env/check`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      try {
        const jsonError = JSON.parse(text);
        return NextResponse.json(jsonError, { status: response.status });
      } catch {
        return NextResponse.json(
          { error: text || 'Failed to check Google Chat environment' },
          { status: response.status }
        );
      }
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/google-chat/env/check] GET Error:', error);
    return NextResponse.json({ error: 'Failed to check Google Chat environment' }, { status: 500 });
  }
}

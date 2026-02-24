import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { searchParams } = new URL(request.url);
    const qs = searchParams.toString();
    const response = await fetch(`${API_BASE_URL}/dynatrace/alerts${qs ? `?${qs}` : ''}`, {
      headers: authResult.headers,
      credentials: 'include',
    });

    if (!response.ok) {
      return NextResponse.json({ error: 'Failed to fetch alerts' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/dynatrace/alerts] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to fetch alerts' }, { status: 500 });
  }
}

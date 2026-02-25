import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await fetch(`${API_BASE_URL}/dynatrace/rca-settings`, {
      headers: authResult.headers,
      credentials: 'include',
    });
    if (!response.ok) {
      return NextResponse.json({ error: 'Failed to get RCA settings' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/dynatrace/rca-settings] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to get RCA settings' }, { status: 500 });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const body = await request.json();
    const response = await fetch(`${API_BASE_URL}/dynatrace/rca-settings`, {
      method: 'PUT',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'include',
    });
    if (!response.ok) {
      return NextResponse.json({ error: 'Failed to update RCA settings' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/dynatrace/rca-settings] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to update RCA settings' }, { status: 500 });
  }
}

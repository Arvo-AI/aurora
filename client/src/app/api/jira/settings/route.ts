import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const response = await fetch(`${API_BASE_URL}/jira/settings`, {
      headers: { ...authHeaders, 'Accept': 'application/json' },
      credentials: 'include',
      cache: 'no-store',
    });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to get settings' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/jira/settings] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to get Jira settings' }, { status: 500 });
  }
}

export async function PATCH(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const payload = await request.json();
    const response = await fetch(`${API_BASE_URL}/jira/settings`, {
      method: 'PATCH',
      headers: { ...authHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      credentials: 'include',
    });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to update settings' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/jira/settings] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to update Jira settings' }, { status: 500 });
  }
}

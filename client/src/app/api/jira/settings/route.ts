import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const response = await fetch(`${API_BASE_URL}/jira/settings`, {
      method: 'GET',
      headers: { ...authHeaders },
    });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to get Jira settings' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/jira/settings] GET error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to get Jira settings' }, { status: 500 });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const payload = await request.json();
    const response = await fetch(`${API_BASE_URL}/jira/settings`, {
      method: 'PUT',
      headers: { ...authHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to update Jira settings' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/jira/settings] PUT error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to update Jira settings' }, { status: 500 });
  }
}

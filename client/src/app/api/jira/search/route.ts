import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const payload = await request.json();
    const response = await fetch(`${API_BASE_URL}/jira/search`, {
      method: 'POST',
      headers: { ...authHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      credentials: 'include',
    });
    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Jira search failed' }, { status: response.status });
    }
    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/jira/search] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Jira search failed' }, { status: 500 });
  }
}

import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;
    const { headers: authHeaders } = authResult;
    const response = await fetch(`${API_BASE_URL}/jira/webhook-url`, {
      method: 'GET',
      headers: { ...authHeaders },
    });
    if (!response.ok) {
      return NextResponse.json({ error: 'Failed to get webhook URL' }, { status: response.status });
    }
    return NextResponse.json(await response.json());
  } catch (error) {
    console.error('[api/jira/webhook-url] GET error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to get webhook URL' }, { status: 500 });
  }
}

import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json(
        { error: 'BACKEND_URL not configured' },
        { status: 500 }
      );
    }

    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { id: incidentId } = await params;
    const body = await request.json();

    const response = await fetch(`${API_BASE_URL}/api/incidents/${incidentId}/merge-alert`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      credentials: 'include',
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      return NextResponse.json(
        { error: data.error || 'Failed to merge alert' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/incidents/[id]/merge-alert] Error:', error);
    return NextResponse.json({ error: 'Failed to merge alert' }, { status: 500 });
  }
}

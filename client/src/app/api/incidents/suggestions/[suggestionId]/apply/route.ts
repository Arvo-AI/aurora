import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ suggestionId: string }> }
) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { suggestionId } = await params;
    const body = await request.json();

    const response = await fetch(
      `${API_BASE_URL}/api/incidents/suggestions/${suggestionId}/apply`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...authHeaders,
        },
        body: JSON.stringify(body),
      }
    );

    // Safe JSON parsing - handle non-JSON responses gracefully
    const text = await response.text();
    let data;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { error: text || 'Unknown error' };
    }
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('Error applying fix suggestion:', error);
    return NextResponse.json(
      { error: 'Failed to apply fix suggestion' },
      { status: 500 }
    );
  }
}

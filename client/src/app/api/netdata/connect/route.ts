import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
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
    const payload = await request.json();

    const response = await fetch(`${API_BASE_URL}/netdata/connect`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      // Log full error server-side for debugging
      const errorText = await response.text();
      console.error('[api/netdata/connect] Backend error:', response.status, errorText);
      // Return sanitized error to client
      return NextResponse.json(
        { error: 'Failed to connect to Netdata' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/netdata/connect] Error:', error);
    return NextResponse.json({ error: 'Failed to connect Netdata' }, { status: 500 });
  }
}

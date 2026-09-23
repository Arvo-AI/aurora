import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const payload = await request.json();

    const response = await fetch(`${API_BASE_URL}/datadog/connect`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      credentials: 'include',
    });

    if (!response.ok) {
      // Forward the parsed body so structured failures (e.g. a 409 label
      // conflict carrying conflictingLabel) survive. Wrapping the raw text in
      // { error } instead would stringify the whole JSON object into the message.
      const text = await response.text();
      try {
        return NextResponse.json(JSON.parse(text), { status: response.status });
      } catch {
        return NextResponse.json({ error: text || 'Failed to connect Datadog' }, { status: response.status });
      }
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/datadog/connect] Error:', error);
    return NextResponse.json({ error: 'Failed to connect Datadog' }, { status: 500 });
  }
}

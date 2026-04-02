import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST() {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    const response = await fetch(`${API_BASE_URL}/grafana/reconnect`, {
      method: 'POST',
      headers: authHeaders,
      credentials: 'include',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to reconnect Grafana' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/grafana/reconnect] Error:', error);
    return NextResponse.json({ error: 'Failed to reconnect Grafana' }, { status: 500 });
  }
}

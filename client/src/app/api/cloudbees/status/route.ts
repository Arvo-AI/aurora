import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: Request) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { searchParams } = new URL(request.url);
    const qs = searchParams.toString();

    const response = await fetch(`${API_BASE_URL}/cloudbees/status${qs ? `?${qs}` : ''}`, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch CloudBees CI status' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/cloudbees/status] Error:', error);
    return NextResponse.json({ error: 'Failed to load CloudBees CI status' }, { status: 500 });
  }
}

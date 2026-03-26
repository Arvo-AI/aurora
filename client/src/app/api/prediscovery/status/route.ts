import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  if (!API_BASE_URL) {
    return NextResponse.json({ error: 'BACKEND_URL not configured' }, { status: 500 });
  }

  const authResult = await getAuthenticatedUser();
  if (authResult instanceof NextResponse) return authResult;

  const response = await fetch(`${API_BASE_URL}/api/prediscovery/status`, {
    method: 'GET',
    headers: authResult.headers,
  });

  const data = await response.json();
  return NextResponse.json(data, { status: response.status });
}

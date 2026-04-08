import { NextResponse, NextRequest } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await fetch(`${API_BASE_URL}/api/mcp/tokens`, {
      headers: { ...authResult.headers },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[api/mcp/tokens] GET Error:', error);
    return NextResponse.json({ error: 'Failed to fetch tokens' }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const body = await req.json();
    const response = await fetch(`${API_BASE_URL}/api/mcp/tokens`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authResult.headers },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[api/mcp/tokens] POST Error:', error);
    return NextResponse.json({ error: 'Failed to create token' }, { status: 500 });
  }
}

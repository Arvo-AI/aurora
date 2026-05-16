import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

async function handleRequest(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
  method: string,
) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { headers: authHeaders } = authResult;
    const { path } = await params;
    const backendPath = path.join('/');
    const qs = request.nextUrl.searchParams.toString();
    const url = qs
      ? `${API_BASE_URL}/opensearch/${backendPath}?${qs}`
      : `${API_BASE_URL}/opensearch/${backendPath}`;

    const options: RequestInit = {
      method,
      headers: authHeaders,
      credentials: 'include',
    };

    if ((method === 'POST' || method === 'PUT') && request.body) {
      const payload = await request.json();
      options.headers = { ...authHeaders, 'Content-Type': 'application/json' };
      options.body = JSON.stringify(payload);
    }

    if (method === 'GET') options.cache = 'no-store';

    const response = await fetch(url, options);

    if (!response.ok) {
      let errorMessage = 'OpenSearch API request failed';
      try {
        const text = await response.text();
        if (text) {
          try {
            const errorData = JSON.parse(text);
            errorMessage = errorData.error || errorData.message || errorMessage;
          } catch {
            if (text.length < 200) errorMessage = text;
          }
        }
      } catch { /* fall back */ }
      return NextResponse.json({ error: errorMessage }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/opensearch] Error:', error);
    return NextResponse.json({ error: 'OpenSearch API request failed' }, { status: 500 });
  }
}

export async function GET(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleRequest(request, ctx, 'GET');
}
export async function POST(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleRequest(request, ctx, 'POST');
}
export async function DELETE(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return handleRequest(request, ctx, 'DELETE');
}

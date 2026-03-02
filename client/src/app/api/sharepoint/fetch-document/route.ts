import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 20000;

export async function POST(request: NextRequest) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json({ error: 'BACKEND_URL is not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    let payload: { driveId?: string; itemId?: string };
    try {
      payload = await request.json();
    } catch {
      return NextResponse.json({ error: 'Invalid JSON payload' }, { status: 400 });
    }

    if (!payload?.driveId || !payload?.itemId) {
      return NextResponse.json({ error: 'driveId and itemId are required' }, { status: 400 });
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/sharepoint/fetch-document`, {
        method: 'POST',
        headers: {
          ...authHeaders,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      // avoid logging raw backend response body
      console.error('[api/sharepoint/fetch-document] Backend error: status=%d', response.status);
      return NextResponse.json({ error: 'Failed to fetch SharePoint document' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.error('[api/sharepoint/fetch-document] Request timeout');
      return NextResponse.json({ error: 'Fetch document request timeout' }, { status: 504 });
    }
    console.error('[api/sharepoint/fetch-document] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to fetch SharePoint document' }, { status: 500 });
  }
}

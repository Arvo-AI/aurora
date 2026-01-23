import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(
  request: NextRequest,
  { params }: { params: { id: string } }
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
    const { id } = await params;
    const body = await request.json();

    if (!body.question?.trim()) {
      return NextResponse.json({ error: 'Missing question' }, { status: 400 });
    }

    // Extract session_id from query params if present
    const { searchParams } = new URL(request.url);
    const sessionId = searchParams.get('session_id');
    
    // Build backend URL with query params if session_id is present
    const backendUrl = sessionId 
      ? `${API_BASE_URL}/api/incidents/${id}/chat?session_id=${sessionId}`
      : `${API_BASE_URL}/api/incidents/${id}/chat`;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000); // 10s timeout for task creation

    try {
      const response = await fetch(backendUrl, {
        method: 'POST',
        headers: {
          ...authHeaders,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          question: body.question,
          mode: body.mode || 'ask',  // Pass mode for execution capability
        }),
        credentials: 'include',
        cache: 'no-store',
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        const text = await response.text();
        return NextResponse.json(
          { error: text || 'Failed to get response' },
          { status: response.status }
        );
      }

      const data = await response.json();
      return NextResponse.json(data);
    } catch (fetchError: unknown) {
      clearTimeout(timeoutId);
      if (fetchError instanceof Error && fetchError.name === 'AbortError') {
        return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
      }
      throw fetchError;
    }
  } catch (error) {
    console.error('[api/incidents/[id]/chat] Error:', error);
    return NextResponse.json({ error: 'Failed to process question' }, { status: 500 });
  }
}

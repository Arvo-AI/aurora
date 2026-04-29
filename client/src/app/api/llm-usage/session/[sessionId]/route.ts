import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isSafeFetchTimeout, safeFetch } from '@/lib/safe-fetch';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { headers: authHeaders } = authResult;
    const { sessionId } = await params;

    const response = await safeFetch(
      `${API_BASE_URL}/api/llm-usage/session/${sessionId}`,
      {
        method: 'GET',
        headers: authHeaders,
        timeoutMs: 10_000,
      }
    );

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return NextResponse.json(
        { error: errorData.error || 'Failed to fetch session usage' },
        { status: response.status }
      );
    }

    return NextResponse.json(await response.json());
  } catch (error) {
    if (isSafeFetchTimeout(error)) {
      return NextResponse.json({ error: 'Backend timeout' }, { status: 504 });
    }
    console.error('Error fetching session usage:', error);
    return NextResponse.json({ error: 'Failed to fetch session usage' }, { status: 500 });
  }
}

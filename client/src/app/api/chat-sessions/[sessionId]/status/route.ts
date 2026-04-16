import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser, makeAuthenticatedRequest } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { sessionId } = await params;

    const response = await makeAuthenticatedRequest(
      `${API_BASE_URL}/chat_api/sessions/${sessionId}/status`,
      { method: 'GET' }
    );

    if (response.status === 404) {
      return NextResponse.json(
        { error: 'Chat session not found' },
        { status: 404 }
      );
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return NextResponse.json(
        { error: errorData.error || 'Failed to fetch session status' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error fetching session status:', error);
    return NextResponse.json(
      { error: 'Failed to fetch session status' },
      { status: 500 }
    );
  }
}

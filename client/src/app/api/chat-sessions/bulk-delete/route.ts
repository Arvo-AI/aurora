import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser, makeAuthenticatedRequest } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

// DELETE /api/chat-sessions/bulk-delete - Delete all chat sessions for a user
export async function DELETE(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    
    // If authResult is a NextResponse (error), return it
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { searchParams } = new URL(request.url);
    const currentSessionId = searchParams.get('current_session_id');

    let url = `${API_BASE_URL}/chat_api/sessions/bulk-delete`;
    if (currentSessionId) {
      url += `?current_session_id=${encodeURIComponent(currentSessionId)}`;
    }

    const response = await makeAuthenticatedRequest(url, { method: 'DELETE' });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error('Backend error response:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to delete all chat sessions' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error deleting all chat sessions:', error);
    return NextResponse.json(
      { error: 'Failed to delete all chat sessions' },
      { status: 500 }
    );
  }
} 
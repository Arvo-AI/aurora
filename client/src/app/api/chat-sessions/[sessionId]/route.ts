import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser, makeAuthenticatedRequest } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

// GET /api/chat-sessions/[sessionId] - Get a specific chat session
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    
    // If authResult is a NextResponse (error), return it
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { sessionId } = await params;
    
    const response = await makeAuthenticatedRequest(
      `${API_BASE_URL}/chat_api/sessions/${sessionId}`,
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
      console.error('Backend error response:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to fetch chat session' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error fetching chat session:', error);
    return NextResponse.json(
      { error: 'Failed to fetch chat session' },
      { status: 500 }
    );
  }
}

// PUT /api/chat-sessions/[sessionId] - Update a specific chat session
export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    
    // If authResult is a NextResponse (error), return it
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { sessionId } = await params;
    const body = await request.json();
    const { title, messages, uiState } = body;

    const response = await makeAuthenticatedRequest(
      `${API_BASE_URL}/chat_api/sessions/${sessionId}`,
      {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          title,
          messages,
          ui_state: uiState
        }),
      }
    );

    if (response.status === 404) {
      return NextResponse.json(
        { error: 'Chat session not found' },
        { status: 404 }
      );
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error('Backend error response:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to update chat session' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error updating chat session:', error);
    return NextResponse.json(
      { error: 'Failed to update chat session' },
      { status: 500 }
    );
  }
}

// DELETE /api/chat-sessions/[sessionId] - Delete a specific chat session
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    
    // If authResult is a NextResponse (error), return it
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { sessionId } = await params;

    const response = await makeAuthenticatedRequest(
      `${API_BASE_URL}/chat_api/sessions/${sessionId}`,
      { method: 'DELETE' }
    );

    if (response.status === 404) {
      return NextResponse.json(
        { error: 'Chat session not found' },
        { status: 404 }
      );
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error('Backend error response:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to delete chat session' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error deleting chat session:', error);
    return NextResponse.json(
      { error: 'Failed to delete chat session' },
      { status: 500 }
    );
  }
} 
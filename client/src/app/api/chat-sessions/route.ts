import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser, makeAuthenticatedRequest } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

// GET /api/chat-sessions - Get all chat sessions for a user
export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    
    // If authResult is a NextResponse (error), return it
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const backendUrl = `${API_BASE_URL}/chat_api/sessions`;

    const response = await makeAuthenticatedRequest(
      backendUrl,
      { method: 'GET' }
    );

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error('Backend error fetching chat sessions:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to fetch chat sessions' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error fetching chat sessions:', error);
    return NextResponse.json(
      { error: 'Failed to fetch chat sessions at ' + API_BASE_URL },
      { status: 500 }
    );
  }
}

// POST /api/chat-sessions - Create a new chat session
export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    
    // If authResult is a NextResponse (error), return it
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const body = await request.json();
    const { title, messages, uiState } = body;

    const response = await makeAuthenticatedRequest(
      `${API_BASE_URL}/chat_api/sessions`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          title,
          messages: messages || [],
          ui_state: uiState || {}
        }),
      }
    );

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error('Backend error response:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to create chat session' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error creating chat session:', error);
    return NextResponse.json(
      { error: 'Failed to create chat session' },
      { status: 500 }
    );
  }
} 
import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

// GET /api/llm-usage/models - Get LLM usage models with billing summary
export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();
    
    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { userId, headers: authHeaders } = authResult;

    const response = await fetch(`${API_BASE_URL}/api/llm-usage/models`, {
      method: 'GET',
      headers: authHeaders,
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error('Backend error fetching LLM usage models:', errorData);
      return NextResponse.json(
        { error: errorData.error || 'Failed to fetch LLM usage data' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error fetching LLM usage models:', error);
    return NextResponse.json(
      { error: 'Failed to fetch LLM usage data' },
      { status: 500 }
    );
  }
}

import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

async function handleRequest(request: NextRequest, method: string) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;

    const options: RequestInit = {
      method,
      headers: authHeaders,
      credentials: 'include',
    };

    // Add body for POST and PATCH requests
    if ((method === 'POST' || method === 'PATCH') && request.body) {
      const payload = await request.json();
      options.headers = {
        ...authHeaders,
        'Content-Type': 'application/json',
      };
      options.body = JSON.stringify(payload);
    }

    // Add cache control for GET
    if (method === 'GET') {
      options.cache = 'no-store';
    }

    const response = await fetch(`${API_BASE_URL}/pagerduty`, options);

    if (!response.ok) {
      let errorMessage = 'PagerDuty API request failed';
      try {
        const text = await response.text();
        if (text) {
          try {
            const errorData = JSON.parse(text);
            errorMessage = errorData.message || errorData.error || errorMessage;
          } catch {
            // If it's not JSON, use the text if it's a reasonable length
            if (text.length < 200) {
              errorMessage = text;
            }
          }
        }
      } catch {
        // Fall back to default message
      }
      return NextResponse.json({ error: errorMessage, message: errorMessage }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/pagerduty] Error:', error);
    return NextResponse.json({ error: 'PagerDuty API request failed' }, { status: 500 });
  }
}

export async function GET(request: NextRequest) {
  return handleRequest(request, 'GET');
}

export async function POST(request: NextRequest) {
  return handleRequest(request, 'POST');
}

export async function PATCH(request: NextRequest) {
  return handleRequest(request, 'PATCH');
}

export async function DELETE(request: NextRequest) {
  return handleRequest(request, 'DELETE');
}


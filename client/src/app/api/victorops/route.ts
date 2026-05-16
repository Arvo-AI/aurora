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

    if ((method === 'POST' || method === 'PATCH') && request.body) {
      const payload = await request.json();
      options.headers = {
        ...authHeaders,
        'Content-Type': 'application/json',
      };
      options.body = JSON.stringify(payload);
    }

    if (method === 'GET') {
      options.cache = 'no-store';
    }

    const response = await fetch(`${API_BASE_URL}/victorops`, options);

    if (!response.ok) {
      let errorMessage = 'Splunk On-Call API request failed';
      try {
        const text = await response.text();
        if (text) {
          try {
            const errorData = JSON.parse(text);
            errorMessage = errorData.message || errorData.error || errorMessage;
          } catch {
            if (text.length < 200) errorMessage = text;
          }
        }
      } catch {
        // fall back to default
      }
      return NextResponse.json(
        { error: errorMessage, message: errorMessage },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/victorops] Error:', error);
    return NextResponse.json(
      { error: 'Splunk On-Call API request failed' },
      { status: 500 }
    );
  }
}

export async function GET(request: NextRequest) {
  return handleRequest(request, 'GET');
}

export async function POST(request: NextRequest) {
  return handleRequest(request, 'POST');
}

export async function DELETE(request: NextRequest) {
  return handleRequest(request, 'DELETE');
}

import { NextRequest } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    if (!API_BASE_URL) return new Response('BACKEND_URL not configured', { status: 500 });

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof Response) return authResult;

    const response = await fetch(`${API_BASE_URL}/api/incidents/stream`, {
      method: 'GET',
      headers: authResult.headers,
      credentials: 'include',
    });

    if (!response.ok) return new Response('Failed to connect to incident stream', { status: response.status });

    return new Response(response.body, {
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive' },
    });
  } catch (error) {
    return new Response('Failed to connect to incident stream', { status: 500 });
  }
}


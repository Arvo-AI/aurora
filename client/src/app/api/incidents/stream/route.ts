import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { relayServerSentEvents } from '@/lib/sse-relay';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    if (!API_BASE_URL) return new Response('BACKEND_URL not configured', { status: 500 });

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const response = await fetch(`${API_BASE_URL}/api/incidents/stream`, {
      method: 'GET',
      headers: authResult.headers,
      credentials: 'include',
      signal: request.signal,
    });

    if (!response.ok) return new Response('Failed to connect to incident stream', { status: response.status });

    const backendBody = response.body;
    if (!backendBody) return new Response('No stream body', { status: 502 });

    return relayServerSentEvents(backendBody, request.signal);
  } catch (error) {
    console.error('[api/incidents/stream] Error:', error);
    return new Response('Failed to connect to incident stream', { status: 500 });
  }
}

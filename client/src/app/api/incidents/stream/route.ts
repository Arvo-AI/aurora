import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    if (!API_BASE_URL) return new Response('BACKEND_URL not configured', { status: 500 });

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10_000);

    const response = await fetch(`${API_BASE_URL}/api/incidents/stream`, {
      method: 'GET',
      headers: authResult.headers,
      credentials: 'include',
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!response.ok) return new Response('Failed to connect to incident stream', { status: response.status });

    const backendBody = response.body;
    if (!backendBody) return new Response('No stream body', { status: 502 });

    const encoder = new TextEncoder();
    const heartbeat = encoder.encode(':heartbeat\n\n');

    const stream = new ReadableStream({
      async start(ctrl) {
        const interval = setInterval(() => {
          try { ctrl.enqueue(heartbeat); } catch { clearInterval(interval); }
        }, 30_000);

        const reader = backendBody.getReader();
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            ctrl.enqueue(value);
          }
          ctrl.close();
        } catch (err) {
          ctrl.error(err);
        } finally {
          clearInterval(interval);
          reader.releaseLock();
        }
      },
      cancel() {
        backendBody.cancel();
      },
    });

    return new Response(stream, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache, no-transform',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      },
    });
  } catch (error) {
    console.error('[api/incidents/stream] Error:', error);
    return new Response('Failed to connect to incident stream', { status: 500 });
  }
}

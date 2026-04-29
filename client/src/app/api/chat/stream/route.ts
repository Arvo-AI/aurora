import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { env } from '@/lib/server-env';

// SSE proxy for the chat transport. Mirrors the pattern in
// /api/incidents/stream/route.ts: heartbeat injection, client-disconnect
// handling, X-Accel-Buffering: no, AbortController on the upstream connect.
// Forwards Last-Event-ID for resume.
export async function GET(request: NextRequest) {
  try {
    if (!env.BACKEND_URL) {
      return new Response('BACKEND_URL not configured', { status: 500 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { searchParams } = new URL(request.url);
    const sessionId = searchParams.get('session_id');
    if (!sessionId) {
      return new Response('session_id is required', { status: 400 });
    }

    const headers: Record<string, string> = { ...authResult.headers };
    if (env.INTERNAL_API_SECRET) {
      headers['X-Internal-Secret'] = env.INTERNAL_API_SECRET;
    }
    const lastEventId = request.headers.get('last-event-id');
    if (lastEventId) headers['Last-Event-ID'] = lastEventId;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10_000);

    const upstreamUrl = `${env.BACKEND_URL}/api/chat/stream?session_id=${encodeURIComponent(sessionId)}`;
    const response = await fetch(upstreamUrl, {
      method: 'GET',
      headers,
      credentials: 'include',
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (response.status === 204) {
      return new Response(null, { status: 204 });
    }
    if (!response.ok) {
      return new Response('Failed to connect to chat stream', { status: response.status });
    }

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

        request.signal.addEventListener('abort', () => {
          clearInterval(interval);
          reader.cancel().catch(() => {});
          try { ctrl.close(); } catch (_) {}
        });

        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            ctrl.enqueue(value);
          }
          ctrl.close();
        } catch (err) {
          if (!request.signal.aborted) ctrl.error(err);
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
    console.error('[api/chat/stream] Error:', error instanceof Error ? error.message : 'unknown');
    return new Response('Failed to connect to chat stream', { status: 500 });
  }
}

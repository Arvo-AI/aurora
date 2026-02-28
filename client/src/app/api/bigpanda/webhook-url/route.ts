import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);

    try {
      const response = await fetch(`${API_BASE_URL}/bigpanda/webhook-url`, {
        method: 'GET',
        headers: authResult.headers,
        credentials: 'include',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        console.error('[api/bigpanda/webhook-url] Backend error:', await response.text());
        return NextResponse.json({ error: 'Failed to get webhook URL' }, { status: response.status });
      }
      return NextResponse.json(await response.json());
    } finally {
      clearTimeout(timeoutId);
    }
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/bigpanda/webhook-url] Error:', error instanceof Error ? error.message : 'Unknown');
    return NextResponse.json({ error: 'Failed to get webhook URL' }, { status: 500 });
  }
}

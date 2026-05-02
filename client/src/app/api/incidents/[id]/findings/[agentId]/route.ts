import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const AGENT_ID_RE = /^[a-zA-Z0-9_-]{1,64}$/;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; agentId: string }> }
) {
  try {
    if (!API_BASE_URL) {
      return NextResponse.json(
        { error: 'BACKEND_URL not configured' },
        { status: 500 }
      );
    }

    const { id, agentId } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: 'Invalid incident id' }, { status: 400 });
    }
    if (!AGENT_ID_RE.test(agentId)) {
      return NextResponse.json({ error: 'Invalid agent id' }, { status: 400 });
    }

    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) {
      return authResult;
    }
    const { headers: authHeaders } = authResult;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30000);

    try {
      const response = await fetch(`${API_BASE_URL}/api/incidents/${id}/findings/${agentId}`, {
        method: 'GET',
        headers: authHeaders,
        credentials: 'include',
        cache: 'no-store',
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        const text = await response.text();
        return NextResponse.json(
          { error: text || 'Failed to get finding' },
          { status: response.status }
        );
      }

      const data = await response.json();
      return NextResponse.json(data);
    } catch (fetchError: unknown) {
      clearTimeout(timeoutId);
      if (fetchError instanceof Error && fetchError.name === 'AbortError') {
        return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
      }
      throw fetchError;
    }
  } catch (error) {
    console.error('[api/incidents/[id]/findings/[agentId]] GET Error:', error);
    return NextResponse.json({ error: 'Failed to get finding' }, { status: 500 });
  }
}

import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;
const FETCH_TIMEOUT_MS = 30000;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ sid: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { sid } = await params;
    const { searchParams } = new URL(request.url);

    // Check if this is a results request
    const isResults = searchParams.get('results') === 'true';
    const offset = searchParams.get('offset') || '0';
    const count = searchParams.get('count') || '1000';

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let url: string;
    if (isResults) {
      url = `${API_BASE_URL}/splunk/search/jobs/${sid}/results?offset=${offset}&count=${count}`;
    } else {
      url = `${API_BASE_URL}/splunk/search/jobs/${sid}`;
    }

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'GET',
        headers: authHeaders,
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      // Log only status code, not potentially sensitive response body
      console.error(`[api/splunk/search/jobs/${sid}] Backend error: status=${response.status}`);
      return NextResponse.json({ error: 'Failed to get job info' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.error('[api/splunk/search/jobs/[sid]] Request timeout');
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/splunk/search/jobs/[sid]] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to get job info' }, { status: 500 });
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ sid: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { sid } = await params;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}/splunk/search/jobs/${sid}`, {
        method: 'DELETE',
        headers: authHeaders,
        credentials: 'include',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (!response.ok) {
      // Log only status code, not potentially sensitive response body
      console.error(`[api/splunk/search/jobs/${sid}] Delete error: status=${response.status}`);
      return NextResponse.json({ error: 'Failed to cancel job' }, { status: response.status });
    }

    // Handle empty responses (204 No Content or empty body)
    const contentLength = response.headers.get('content-length');
    if (response.status === 204 || contentLength === '0') {
      return NextResponse.json({ success: true });
    }

    const text = await response.text();
    if (!text) {
      return NextResponse.json({ success: true });
    }

    const data = JSON.parse(text);
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      console.error('[api/splunk/search/jobs/[sid]] Request timeout');
      return NextResponse.json({ error: 'Request timeout' }, { status: 504 });
    }
    console.error('[api/splunk/search/jobs/[sid]] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Failed to cancel job' }, { status: 500 });
  }
}

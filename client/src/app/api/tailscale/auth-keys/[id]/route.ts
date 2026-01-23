import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { id } = await params;
    const { searchParams } = new URL(request.url);
    const tailnet = searchParams.get('tailnet');

    const url = tailnet
      ? `${API_BASE_URL}/tailscale_api/tailscale/auth-keys/${id}?tailnet=${encodeURIComponent(tailnet)}`
      : `${API_BASE_URL}/tailscale_api/tailscale/auth-keys/${id}`;

    const response = await fetch(url, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch auth key' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/tailscale/auth-keys/[id]] Error:', error);
    return NextResponse.json({ error: 'Failed to load auth key' }, { status: 500 });
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { id } = await params;
    const { searchParams } = new URL(request.url);
    const tailnet = searchParams.get('tailnet');

    const url = tailnet
      ? `${API_BASE_URL}/tailscale_api/tailscale/auth-keys/${id}?tailnet=${encodeURIComponent(tailnet)}`
      : `${API_BASE_URL}/tailscale_api/tailscale/auth-keys/${id}`;

    const response = await fetch(url, {
      method: 'DELETE',
      headers: authHeaders,
      credentials: 'include',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to delete auth key' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/tailscale/auth-keys/[id]] Error:', error);
    return NextResponse.json({ error: 'Failed to delete auth key' }, { status: 500 });
  }
}

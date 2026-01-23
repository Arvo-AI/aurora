import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { searchParams } = new URL(request.url);
    const tailnet = searchParams.get('tailnet');

    const url = tailnet
      ? `${API_BASE_URL}/tailscale_api/tailscale/acl?tailnet=${encodeURIComponent(tailnet)}`
      : `${API_BASE_URL}/tailscale_api/tailscale/acl`;

    const response = await fetch(url, {
      method: 'GET',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to fetch ACL' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/tailscale/acl] Error:', error);
    return NextResponse.json({ error: 'Failed to load ACL' }, { status: 500 });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const { searchParams } = new URL(request.url);
    const tailnet = searchParams.get('tailnet');
    const payload = await request.json();

    const url = tailnet
      ? `${API_BASE_URL}/tailscale_api/tailscale/acl?tailnet=${encodeURIComponent(tailnet)}`
      : `${API_BASE_URL}/tailscale_api/tailscale/acl`;

    const response = await fetch(url, {
      method: 'PUT',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      credentials: 'include',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to update ACL' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/tailscale/acl] Error:', error);
    return NextResponse.json({ error: 'Failed to update ACL' }, { status: 500 });
  }
}

import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

/**
 * Disconnects Datadog. With ?account=<label> only that organization is removed;
 * without it every organization is removed.
 *
 * The shared /api/connected-accounts/[provider] route cannot be reused here: it
 * issues a bare DELETE and forwards no query string, so the account selector
 * would be dropped and every org removed.
 */
export async function DELETE(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { headers: authHeaders } = authResult;
    const account = new URL(request.url).searchParams.get('account');
    const url = account
      ? `${API_BASE_URL}/datadog/disconnect?account=${encodeURIComponent(account)}`
      : `${API_BASE_URL}/datadog/disconnect`;

    const response = await fetch(url, {
      method: 'DELETE',
      headers: authHeaders,
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      return NextResponse.json({ error: text || 'Failed to disconnect Datadog' }, { status: response.status });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[api/datadog/disconnect] Error:', error);
    return NextResponse.json({ error: 'Failed to disconnect Datadog' }, { status: 500 });
  }
}

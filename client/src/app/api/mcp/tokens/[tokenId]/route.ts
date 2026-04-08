import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function DELETE(_req: Request, { params }: { params: Promise<{ tokenId: string }> }) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { tokenId } = await params;
    const response = await fetch(`${API_BASE_URL}/api/mcp/tokens/${tokenId}`, {
      method: 'DELETE',
      headers: { ...authResult.headers },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[api/mcp/tokens/delete] Error:', error);
    return NextResponse.json({ error: 'Failed to revoke token' }, { status: 500 });
  }
}

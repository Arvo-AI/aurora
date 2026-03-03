import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const API_BASE_URL = process.env.BACKEND_URL;

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ userId: string; role: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser();
    if (authResult instanceof NextResponse) return authResult;

    const { userId, role } = await params;
    const response = await fetch(`${API_BASE_URL}/api/admin/users/${userId}/roles/${role}`, {
      method: 'DELETE',
      headers: { ...authResult.headers },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[api/admin/users/roles] DELETE Error:', error);
    return NextResponse.json({ error: 'Failed to revoke role' }, { status: 500 });
  }
}

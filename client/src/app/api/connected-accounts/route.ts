import { NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

const backendUrl = process.env.BACKEND_URL;

export async function GET() {
  try {
    if (!backendUrl) {
      return NextResponse.json({ error: 'Backend URL not configured' }, { status: 500 });
    }

    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult;
    }

    const { userId, headers: authHeaders } = authResult;

    const response = await fetch(`${backendUrl}/api/connected-accounts/${userId}`, {
      method: 'GET',
      headers: authHeaders,
    });

    if (!response.ok) {
      throw new Error(`Failed to fetch account info: ${response.status}`);
    }

    const accountData = await response.json();
    return NextResponse.json(accountData);
  } catch (error: any) {
    console.error('Error fetching connected accounts:', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
} 
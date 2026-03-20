import { NextRequest, NextResponse } from 'next/server';
import { auth } from '@/auth';

const backendUrl = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  try {
    if (!backendUrl) {
      return NextResponse.json({ error: 'Backend URL not configured' }, { status: 500 });
    }

    const session = await auth();

    if (!session?.userId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }

    const body = await request.json();

    const response = await fetch(`${backendUrl}/api/auth/setup-org`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-ID': session.userId,
      },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error: any) {
    console.error('Error setting up org:', error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}

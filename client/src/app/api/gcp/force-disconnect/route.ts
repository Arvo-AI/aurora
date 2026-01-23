import { NextResponse } from 'next/server';
import { auth } from '@/auth';

const API_BASE_URL = process.env.NEXT_PUBLIC_BACKEND_URL;

export async function POST() {
  try {
    const session = await auth();

    if (!session?.userId) {
      return NextResponse.json(
        { error: 'Unauthorized' },
        { status: 401 }
      );
    }

    const userId = session.userId;

    // Call backend to delete GCP tokens
    const backendResp = await fetch(
      `${API_BASE_URL}/api/gcp/force-disconnect`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
      }
    );

    if (!backendResp.ok) {
      const errorData = await backendResp.json().catch(() => ({ error: 'Failed to disconnect GCP' }));
      return NextResponse.json(
        { error: errorData.error || 'Failed to disconnect GCP' },
        { status: backendResp.status }
      );
    }

    const data = await backendResp.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error in force-disconnect API route:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
}

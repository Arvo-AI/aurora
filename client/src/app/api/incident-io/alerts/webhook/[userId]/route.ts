import { NextRequest, NextResponse } from 'next/server';

const API_BASE_URL = process.env.BACKEND_URL;

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> }
) {
  const { userId } = await params;

  try {
    const body = await request.text();

    const response = await fetch(`${API_BASE_URL}/incidentio/alerts/webhook/${userId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[api/incident-io/webhook] Error:', error instanceof Error ? error.message : 'Unknown error');
    return NextResponse.json({ error: 'Webhook proxy failed' }, { status: 502 });
  }
}

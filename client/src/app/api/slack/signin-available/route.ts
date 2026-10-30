import { NextResponse } from 'next/server';

const API_BASE_URL = process.env.BACKEND_URL;

// Public: the sign-in page asks whether to show "Continue with Slack".
export async function GET() {
  if (!API_BASE_URL) return NextResponse.json({ available: false });
  try {
    const response = await fetch(`${API_BASE_URL}/slack/signin/available`, {
      signal: AbortSignal.timeout(5000),
      cache: 'no-store',
    });
    if (!response.ok) return NextResponse.json({ available: false });
    const data = await response.json();
    return NextResponse.json({ available: data.available === true });
  } catch {
    return NextResponse.json({ available: false });
  }
}

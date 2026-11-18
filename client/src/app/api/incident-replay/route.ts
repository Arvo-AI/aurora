import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';

export const runtime = 'nodejs';
const BACKEND_URL = process.env.BACKEND_URL;

export async function POST(request: NextRequest) {
  if (process.env.INCIDENT_REPLAY_ENABLED !== 'true') {
    return NextResponse.json({ error: 'Incident replay is disabled.' }, { status: 404 });
  }
  const auth = await getAuthenticatedUser();
  if (auth instanceof NextResponse) return auth;
  if (!BACKEND_URL) return NextResponse.json({error:'Backend unavailable'}, {status:503});
  // Bound the request before forwarding it to Aurora's authenticated backend.
  const size = Number(request.headers.get('content-length') || '0');
  if (size > 16000) return NextResponse.json({ error: 'Payload too large.' }, {status:413});
  try {
    const body = await request.text();
    if (body.length > 16000) return NextResponse.json({error:'Payload too large.'}, {status:413});
    const response = await fetch(`${BACKEND_URL}/api/incident-replay`, {
      method: 'POST',
      headers: {...auth.headers, 'Content-Type': 'application/json'},
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(300000),
    });
    const data = await response.json().catch(() => ({error:'Invalid backend response'}));
    return NextResponse.json(data, {status:response.status, headers:{'Cache-Control':'no-store'}});
  } catch (error) {
    // Log the proxy failure without logging incident evidence or credentials.
    console.error('Incident Replay proxy failed:', error);
    return NextResponse.json({error:'Investigation service unavailable.'}, {status:502});
  }
}

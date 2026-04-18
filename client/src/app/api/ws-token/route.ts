import { NextResponse } from 'next/server';
import { SignJWT } from 'jose';
import { auth } from '@/auth';

const INTERNAL_API_SECRET = process.env.INTERNAL_API_SECRET || '';

export async function GET() {
  const session = await auth();

  if (!session?.userId) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  if (!INTERNAL_API_SECRET) {
    return NextResponse.json(
      { error: 'Server misconfiguration: INTERNAL_API_SECRET not set' },
      { status: 500 },
    );
  }

  const secret = new TextEncoder().encode(INTERNAL_API_SECRET);

  const token = await new SignJWT({
    userId: session.userId,
    orgId: session.orgId ?? null,
    jti: crypto.randomUUID(),
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuedAt()
    .setExpirationTime('60s')
    .sign(secret);

  return NextResponse.json({ token });
}

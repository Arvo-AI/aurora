import { NextRequest, NextResponse } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  if (path.some((segment) => segment === '.' || segment === '..' || segment.includes('/'))) {
    return NextResponse.json({ error: 'Invalid onboarding path' }, { status: 400 });
  }
  const backendPath = `/api/onboarding/${path.map(encodeURIComponent).join('/')}`;
  return forwardRequest(request, request.method, backendPath, 'onboarding');
}

export { handler as GET, handler as POST, handler as PUT, handler as DELETE };

import { NextRequest, NextResponse } from 'next/server'
import { forwardPublicRequest } from '@/lib/backend-proxy'

// Public by design: the caller proves who they are with the emailed reset code,
// not with a session. All validation (code freshness, attempt count, single-use
// burning) happens on the backend.
export async function POST(request: NextRequest): Promise<NextResponse> {
  return forwardPublicRequest(request, 'POST', '/api/auth/reset-password', 'reset-password')
}

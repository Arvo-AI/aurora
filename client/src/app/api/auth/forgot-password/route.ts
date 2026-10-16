import { NextRequest, NextResponse } from 'next/server'
import { forwardPublicRequest } from '@/lib/backend-proxy'

// Public by design: the caller is locked out of their account, so there is no
// session to forward. The backend answers 200 with the same message whether or
// not the account exists, and the proxy passes its body through untouched so we
// don't leak the difference.
export async function POST(request: NextRequest): Promise<NextResponse> {
  return forwardPublicRequest(request, 'POST', '/api/auth/forgot-password', 'forgot-password')
}

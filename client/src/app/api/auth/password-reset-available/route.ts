import { NextRequest, NextResponse } from 'next/server'
import { forwardPublicRequest } from '@/lib/backend-proxy'

// Public by design, and safe to be: the answer is about this deployment's SMTP
// configuration, not about any account, so it gives an attacker nothing to
// enumerate. The sign-in page uses it to avoid offering a reset it can't deliver.
export async function GET(request: NextRequest): Promise<NextResponse> {
  return forwardPublicRequest(request, 'GET', '/api/auth/password-reset-available', 'password-reset-available')
}

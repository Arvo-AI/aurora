import { NextRequest, NextResponse } from 'next/server'
import { forwardPublicRequest } from '@/lib/backend-proxy'

// Public by design: the caller is choosing how to sign in, so there is no
// session yet. Returns the SAML login URL for the org that verified the
// email's domain, or 404 when SSO isn't set up for it.
export async function POST(request: NextRequest): Promise<NextResponse> {
  return forwardPublicRequest(request, 'POST', '/api/auth/saml/discover', 'sso-discover')
}

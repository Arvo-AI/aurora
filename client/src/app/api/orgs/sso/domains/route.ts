import { NextRequest, NextResponse } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function POST(request: NextRequest): Promise<NextResponse> {
  return forwardRequest(request, 'POST', '/api/orgs/sso/domains', 'sso-domains')
}

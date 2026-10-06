import { NextRequest, NextResponse } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

type Params = { params: Promise<{ domainId: string }> }

export async function POST(request: NextRequest, { params }: Params): Promise<NextResponse> {
  const { domainId } = await params
  return forwardRequest(request, 'POST', `/api/orgs/sso/domains/${encodeURIComponent(domainId)}/verify`, 'sso-domains')
}

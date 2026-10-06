import { NextRequest, NextResponse } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

type Params = { params: Promise<{ domainId: string }> }

export async function DELETE(request: NextRequest, { params }: Params): Promise<NextResponse> {
  const { domainId } = await params
  return forwardRequest(request, 'DELETE', `/api/orgs/sso/domains/${encodeURIComponent(domainId)}`, 'sso-domains')
}

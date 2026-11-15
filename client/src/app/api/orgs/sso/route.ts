import { NextRequest, NextResponse } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function GET(request: NextRequest): Promise<NextResponse> {
  return forwardRequest(request, 'GET', '/api/orgs/sso', 'sso-settings')
}

export async function PUT(request: NextRequest): Promise<NextResponse> {
  return forwardRequest(request, 'PUT', '/api/orgs/sso', 'sso-settings')
}

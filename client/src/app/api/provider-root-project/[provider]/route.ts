import { NextRequest, NextResponse } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

const BACKEND_ENDPOINTS: Record<string, string> = {
  ovh: '/ovh_api/ovh/root-project',
  scaleway: '/scaleway_api/scaleway/root-project',
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ provider: string }> }
) {
  const { provider } = await params
  const backendPath = BACKEND_ENDPOINTS[provider]

  if (!backendPath) {
    return NextResponse.json({ error: `Unsupported provider: ${provider}` }, { status: 400 })
  }

  return forwardRequest(request, 'GET', backendPath, `${provider}-root-project`)
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ provider: string }> }
) {
  const { provider } = await params
  const backendPath = BACKEND_ENDPOINTS[provider]

  if (!backendPath) {
    return NextResponse.json({ error: `Unsupported provider: ${provider}` }, { status: 400 })
  }

  return forwardRequest(request, 'POST', backendPath, `${provider}-root-project`)
}

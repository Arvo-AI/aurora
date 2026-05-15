import { NextRequest } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/scaleway_api/scaleway/projects', 'scaleway-projects')
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/scaleway_api/scaleway/projects', 'scaleway-projects')
}

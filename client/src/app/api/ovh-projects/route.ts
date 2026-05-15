import { NextRequest } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/ovh_api/ovh/projects', 'ovh-projects')
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/ovh_api/ovh/projects', 'ovh-projects')
}

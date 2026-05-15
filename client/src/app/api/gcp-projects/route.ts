import { NextRequest } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/gcp/sa-project-access', 'gcp-projects')
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/gcp/sa-project-access', 'gcp-projects')
}

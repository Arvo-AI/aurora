import { NextRequest } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'

export async function GET(request: NextRequest) {
  return forwardRequest(request, 'GET', '/api/azure-subscriptions', 'azure-subscriptions')
}

export async function POST(request: NextRequest) {
  return forwardRequest(request, 'POST', '/api/azure-subscriptions', 'azure-subscriptions')
}

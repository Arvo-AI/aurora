import { NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function GET() {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const response = await fetch(`${API_BASE_URL}/api/orgs/my-invitations`, {
    headers: authResult.headers,
    cache: 'no-store',
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

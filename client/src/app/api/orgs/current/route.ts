import { NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function GET() {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const response = await fetch(`${API_BASE_URL}/api/orgs/current`, {
    headers: authResult.headers,
    cache: 'no-store',
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

export async function PATCH(request: Request) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const body = await request.json()
  const response = await fetch(`${API_BASE_URL}/api/orgs`, {
    method: 'PATCH',
    headers: { ...authResult.headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

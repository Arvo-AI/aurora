import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function POST(req: NextRequest) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const body = await req.json()

  const response = await fetch(`${API_BASE_URL}/api/orgs/track`, {
    method: 'POST',
    headers: { ...authResult.headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

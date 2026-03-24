import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function POST(request: NextRequest) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const body = await request.json()
  const safeBody: Record<string, string> = {}
  if (body.invitation_id) safeBody.invitation_id = String(body.invitation_id)

  const response = await fetch(`${API_BASE_URL}/api/orgs/join`, {
    method: 'POST',
    headers: { ...authResult.headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(safeBody),
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

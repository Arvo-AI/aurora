import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function POST(request: NextRequest) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  let body: Record<string, unknown>
  try {
    body = await request.json()
  } catch {
    return NextResponse.json({ error: 'Invalid request body' }, { status: 400 })
  }

  const safeBody: Record<string, string> = {}
  if (body.invitation_id) safeBody.invitation_id = String(body.invitation_id)

  const response = await fetch(`${API_BASE_URL}/api/orgs/join`, {
    method: 'POST',
    headers: { ...authResult.headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(safeBody),
  })

  let data: unknown
  try {
    data = await response.json()
  } catch {
    return NextResponse.json({ error: 'Backend returned invalid response' }, { status: 502 })
  }

  return NextResponse.json(data, { status: response.status })
}

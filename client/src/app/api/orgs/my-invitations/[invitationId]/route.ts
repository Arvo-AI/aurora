import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ invitationId: string }> }
) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const { invitationId } = await params

  let body: Record<string, unknown>
  try {
    body = await request.json()
  } catch {
    return NextResponse.json({ error: 'Invalid request body' }, { status: 400 })
  }

  const action = body.action
  if (action !== 'accept' && action !== 'decline') {
    return NextResponse.json({ error: 'action must be "accept" or "decline"' }, { status: 400 })
  }

  if (action === 'accept') {
    const response = await fetch(`${API_BASE_URL}/api/orgs/join`, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ invitation_id: invitationId }),
    })
    const data = await response.json().catch(() => ({ error: 'Backend returned invalid response' }))
    return NextResponse.json(data, { status: response.status })
  }

  const response = await fetch(
    `${API_BASE_URL}/api/orgs/my-invitations/${invitationId}/decline`,
    {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
    },
  )
  const data = await response.json().catch(() => ({ error: 'Backend returned invalid response' }))
  return NextResponse.json(data, { status: response.status })
}

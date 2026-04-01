import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ invitationId: string }> }
) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const { invitationId } = await params

  const response = await fetch(
    `${API_BASE_URL}/api/orgs/invitations/${invitationId}/cancel`,
    {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
    },
  )

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

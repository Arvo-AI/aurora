import { NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ userId: string }> },
) {
  const authResult = await getAuthenticatedUser()
  if (authResult instanceof NextResponse) return authResult

  const { userId } = await params
  const response = await fetch(`${API_BASE_URL}/api/orgs/members/${userId}`, {
    method: 'DELETE',
    headers: authResult.headers,
  })

  const data = await response.json()
  return NextResponse.json(data, { status: response.status })
}

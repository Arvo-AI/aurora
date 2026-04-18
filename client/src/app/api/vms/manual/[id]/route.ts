import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'
import { env } from '@/lib/server-env'

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult
    const { id } = await params
    const body = await request.json()

    const backendResp = await fetch(`${env.BACKEND_URL}/api/vms/manual/${id}`, {
      method: 'PUT',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })

    const data = await backendResp.json()
    return NextResponse.json(data, { status: backendResp.status })
  } catch (error) {
    console.error('Error updating manual VM:', error)
    return NextResponse.json(
      { error: 'Failed to update VM' },
      { status: 500 }
    )
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult
    const { id } = await params

    const backendResp = await fetch(`${env.BACKEND_URL}/api/vms/manual/${id}`, {
      method: 'DELETE',
      headers: authHeaders,
    })

    const data = await backendResp.json()
    return NextResponse.json(data, { status: backendResp.status })
  } catch (error) {
    console.error('Error deleting manual VM:', error)
    return NextResponse.json(
      { error: 'Failed to delete VM' },
      { status: 500 }
    )
  }
}

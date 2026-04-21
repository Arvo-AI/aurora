import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'
import { env } from '@/lib/server-env'

export async function GET(
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

    const backendResp = await fetch(`${env.BACKEND_URL}/api/ssh-keys/${id}`, {
      method: 'GET',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error fetching SSH key:', backendResp.status, errorText)
      return NextResponse.json(
        { error: 'Failed to fetch SSH key' },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error fetching SSH key:', error)
    return NextResponse.json(
      { error: 'Failed to fetch SSH key' },
      { status: 500 }
    )
  }
}

export async function PATCH(
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

    const backendResp = await fetch(`${env.BACKEND_URL}/api/ssh-keys/${id}`, {
      method: 'PATCH',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error renaming SSH key:', backendResp.status, errorText)
      return NextResponse.json(
        { error: 'Failed to rename SSH key' },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error renaming SSH key:', error)
    return NextResponse.json(
      { error: 'Failed to rename SSH key' },
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

    const backendResp = await fetch(`${env.BACKEND_URL}/api/ssh-keys/${id}`, {
      method: 'DELETE',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error deleting SSH key:', backendResp.status, errorText)
      return NextResponse.json(
        { error: 'Failed to delete SSH key' },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error deleting SSH key:', error)
    return NextResponse.json(
      { error: 'Failed to delete SSH key' },
      { status: 500 }
    )
  }
}

import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

// BACKEND_URL for server-side API calls; fallback to public URL for local dev
const API_BASE_URL = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL

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

    const backendResp = await fetch(`${API_BASE_URL}/api/ssh-keys/${id}`, {
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

    const backendResp = await fetch(`${API_BASE_URL}/api/ssh-keys/${id}`, {
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

    const backendResp = await fetch(`${API_BASE_URL}/api/ssh-keys/${id}`, {
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

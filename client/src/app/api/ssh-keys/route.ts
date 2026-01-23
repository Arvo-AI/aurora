import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

// BACKEND_URL for server-side API calls; fallback to public URL for local dev
const API_BASE_URL = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult

    const backendResp = await fetch(`${API_BASE_URL}/api/ssh-keys`, {
      method: 'GET',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error fetching SSH keys:', backendResp.status, errorText)
      return NextResponse.json(
        { error: 'Failed to fetch SSH keys', keys: [] },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error fetching SSH keys:', error)
    return NextResponse.json(
      { error: 'Failed to fetch SSH keys', keys: [] },
      { status: 500 }
    )
  }
}

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult

    const backendResp = await fetch(`${API_BASE_URL}/api/ssh-keys`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error creating SSH key:', backendResp.status, errorText)
      return NextResponse.json(
        { error: 'Failed to create SSH key' },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data, { status: 201 })
  } catch (error) {
    console.error('Error creating SSH key:', error)
    return NextResponse.json(
      { error: 'Failed to create SSH key' },
      { status: 500 }
    )
  }
}


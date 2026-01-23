import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult
    const body = await request.json()

    const backendResp = await fetch(`${API_BASE_URL}/api/kubectl/tokens`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })

    if (!backendResp.ok) {
      const text = await backendResp.text()
      throw new Error(`Backend returned ${backendResp.status}: ${text}`)
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error creating kubectl token:', error)
    return NextResponse.json(
      { error: 'Failed to create token' },
      { status: 500 }
    )
  }
}

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult

    const backendResp = await fetch(`${API_BASE_URL}/api/kubectl/tokens`, {
      method: 'GET',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const text = await backendResp.text()
      throw new Error(`Backend returned ${backendResp.status}: ${text}`)
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error fetching kubectl tokens:', error)
    return NextResponse.json(
      { error: 'Failed to fetch tokens' },
      { status: 500 }
    )
  }
}


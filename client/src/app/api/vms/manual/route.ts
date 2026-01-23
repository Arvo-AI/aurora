import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

// BACKEND_URL for server-side API calls; fallback to public URL for local dev
const API_BASE_URL = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL

export async function GET() {
  try {
    // Check if backend URL is configured
    if (!API_BASE_URL) {
      console.error('BACKEND_URL environment variable is not set')
      return NextResponse.json(
        { error: 'Server configuration error', vms: [] },
        { status: 500 }
      )
    }

    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult

    const backendResp = await fetch(`${API_BASE_URL}/api/vms/manual`, {
      method: 'GET',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error fetching manual VMs:', backendResp.status, errorText)
      return NextResponse.json(
        { error: 'Failed to fetch VMs', vms: [] },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error: any) {
    console.error('Error fetching manual VMs:', error?.message || error)
    console.error('BACKEND_URL was:', API_BASE_URL || 'NOT SET')
    return NextResponse.json(
      { error: error?.message || 'Failed to fetch VMs', vms: [] },
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
    const body = await request.json()

    const backendResp = await fetch(`${API_BASE_URL}/api/vms/manual`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })

    const data = await backendResp.json()
    return NextResponse.json(data, { status: backendResp.status })
  } catch (error) {
    console.error('Error creating manual VM:', error)
    return NextResponse.json(
      { error: 'Failed to create VM' },
      { status: 500 }
    )
  }
}


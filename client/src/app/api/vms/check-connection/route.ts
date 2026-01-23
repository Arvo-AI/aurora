import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

// BACKEND_URL for server-side API calls; fallback to public URL for local dev
const API_BASE_URL = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult
    const body = await request.json()

    const backendResp = await fetch(`${API_BASE_URL}/api/vms/check-connection`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })

    const data = await backendResp.json()
    
    // Pass through the response as-is (backend returns {success, error, connectedAs})
    // even for 4xx status codes so the frontend gets the actual error message
    return NextResponse.json(data, { status: backendResp.status })
  } catch (error) {
    console.error('Error checking connection:', error)
    return NextResponse.json(
      { error: 'Failed to check connection' },
      { status: 500 }
    )
  }
}


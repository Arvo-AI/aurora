import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()
    
    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult

    const backendResp = await fetch(`${API_BASE_URL}/api/kubectl/connections`, {
      method: 'GET',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error fetching kubectl connections:', backendResp.status, errorText)
      
      return NextResponse.json(
        { error: 'Failed to fetch connections', connections: [] },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error fetching kubectl connections:', error)
    return NextResponse.json(
      { error: 'Failed to fetch connections', connections: [] },
      { status: 500 }
    )
  }
}


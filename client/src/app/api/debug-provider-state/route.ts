import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function GET(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult // Return the error response
    }

    const { userId, headers: authHeaders } = authResult

    // Prepare backend request headers (no debug password required)
    const backendHeaders = {
      ...authHeaders,
    }

    const response = await fetch(`${API_BASE_URL}/debug_api/debug-provider-state`, {
      method: 'GET',
      headers: backendHeaders,
    })

    if (!response.ok) {
      const errorText = await response.text()
      console.error('Backend error:', errorText)
      
      // Handle unauthorized errors
      if (response.status === 401) {
        return NextResponse.json(
          { error: 'Unauthorized access to debug data' },
          { status: 401 }
        )
      }
      
      return NextResponse.json(
        { error: 'Failed to fetch debug data from backend' },
        { status: response.status }
      )
    }

    const data = await response.json()
    return NextResponse.json(data)

  } catch (err) {
    console.error('Error in debug-provider-state route:', err)
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    )
  }
}

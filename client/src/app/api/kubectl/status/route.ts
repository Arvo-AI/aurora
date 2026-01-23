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
    const searchParams = request.nextUrl.searchParams
    const token = searchParams.get('token')
    
    const url = token 
      ? `${API_BASE_URL}/api/kubectl/connections?token=${encodeURIComponent(token)}`
      : `${API_BASE_URL}/api/kubectl/connections`

    const backendResp = await fetch(url, {
      method: 'GET',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error checking kubectl status:', backendResp.status, errorText)
      
      if (backendResp.status >= 500) {
        return NextResponse.json(
          { error: 'Backend service unavailable', connected: false },
          { status: backendResp.status }
        )
      }
      
      return NextResponse.json({ connected: false })
    }

    const data = await backendResp.json()
    const connected = data.connections && data.connections.length > 0
    
    return NextResponse.json({ connected })
  } catch (error) {
    console.error('Error checking kubectl status:', error)
    return NextResponse.json(
      { error: 'Failed to check kubectl status', connected: false },
      { status: 503 }
    )
  }
}


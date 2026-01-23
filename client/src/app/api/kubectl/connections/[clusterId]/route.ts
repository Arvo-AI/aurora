import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

export async function DELETE(
  request: NextRequest,
  { params }: { params: { clusterId: string } }
) {
  try {
    const authResult = await getAuthenticatedUser()
    
    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult
    const { clusterId } = params

    const backendResp = await fetch(`${API_BASE_URL}/api/kubectl/connections/${clusterId}`, {
      method: 'DELETE',
      headers: authHeaders,
    })

    if (!backendResp.ok) {
      const errorText = await backendResp.text()
      console.error('Backend error disconnecting cluster:', backendResp.status, errorText)
      
      return NextResponse.json(
        { error: 'Failed to disconnect cluster' },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error disconnecting kubectl cluster:', error)
    return NextResponse.json(
      { error: 'Failed to disconnect cluster' },
      { status: 500 }
    )
  }
}


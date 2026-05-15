import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'
import { env } from '@/lib/server-env'

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { headers: authHeaders } = authResult

    const backendResp = await fetch(`${env.BACKEND_URL}/api/vms/manual`, {
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

    const backendResp = await fetch(`${env.BACKEND_URL}/api/vms/manual`, {
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

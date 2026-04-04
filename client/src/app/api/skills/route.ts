import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function GET() {
  try {
    const authResult = await getAuthenticatedUser()
    if (authResult instanceof NextResponse) return authResult

    const response = await fetch(`${API_BASE_URL}/api/skills/`, {
      headers: { ...authResult.headers },
      cache: 'no-store',
    })

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/skills] GET error:', error instanceof Error ? error.message : 'Unknown')
    return NextResponse.json({ error: 'Failed to fetch skills' }, { status: 500 })
  }
}

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()
    if (authResult instanceof NextResponse) return authResult

    const body = await request.json()
    const response = await fetch(`${API_BASE_URL}/api/skills/`, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/skills] POST error:', error instanceof Error ? error.message : 'Unknown')
    return NextResponse.json({ error: 'Failed to create skill' }, { status: 500 })
  }
}

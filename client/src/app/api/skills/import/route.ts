import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()
    if (authResult instanceof NextResponse) return authResult

    const body = await request.json()
    const { action, ...payload } = body

    const endpoint = action === 'install'
      ? `${API_BASE_URL}/api/skills/import/install`
      : `${API_BASE_URL}/api/skills/import/discover`

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/skills/import] POST error:', error instanceof Error ? error.message : 'Unknown')
    return NextResponse.json({ error: 'Failed to import skills' }, { status: 500 })
  }
}

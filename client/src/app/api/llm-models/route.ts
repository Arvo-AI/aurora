import { NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

// ---------------------------------------------------------------------------
// GET /api/llm-models
// Returns the user-selectable model catalog (with display metadata), honoring
// the backend's ENABLED_MODELS allowlist. Proxies to the viewer-accessible
// /api/llm-config/models backend endpoint.
// ---------------------------------------------------------------------------
export async function GET() {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult // 401
    }

    const { headers } = authResult
    const API_BASE_URL = process.env.BACKEND_URL

    const response = await fetch(`${API_BASE_URL}/api/llm-config/models`, {
      headers,
    })

    if (!response.ok) {
      const error = await response.json().catch(() => ({ error: 'Failed to fetch models' }))
      return NextResponse.json(error, { status: response.status })
    }

    const data = await response.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error fetching model catalog:', error)
    return NextResponse.json({ error: 'Failed to fetch models' }, { status: 500 })
  }
}

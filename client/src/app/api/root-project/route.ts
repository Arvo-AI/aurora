import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser, makeAuthenticatedRequest } from '@/lib/auth-helper'

// ---------------------------------------------------------------------------
// GET /api/root-project
// Gets the current root project for the user
// ---------------------------------------------------------------------------
export async function GET() {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult // Return the error response
    }

    const { userId, headers } = authResult
    const API_BASE_URL = process.env.BACKEND_URL

    const response = await fetch(`${API_BASE_URL}/api/gcp/root-project`, {
      headers
    })

    if (!response.ok) {
      const error = await response.json()
      return NextResponse.json(error, { status: response.status })
    }

    const data = await response.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error getting root project:', error)
    return NextResponse.json(
      { error: 'Failed to get root project' },
      { status: 500 }
    )
  }
}

// ---------------------------------------------------------------------------
// POST /api/root-project
// Sets the root project for service account creation
// ---------------------------------------------------------------------------
export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult // Return the error response
    }

    const { userId, headers } = authResult
    const API_BASE_URL = process.env.BACKEND_URL

    // Get the request body
    const body = await request.json()
    const { projectId } = body

    if (!projectId) {
      return NextResponse.json(
        { error: 'Missing projectId in request body' },
        { status: 400 }
      )
    }

    // Call backend with proper authentication
    const response = await fetch(`${API_BASE_URL}/api/gcp/root-project`, {
      method: 'POST',
      headers: {
        ...headers,
'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        project_id: projectId
      })
    })

    if (!response.ok) {
      const error = await response.json()
      return NextResponse.json(error, { status: response.status })
    }

    const data = await response.json()
    return NextResponse.json(data)
  } catch (error) {
    console.error('Error setting root project:', error)
    return NextResponse.json(
      { error: 'Failed to set root project' },
      { status: 500 }
    )
  }
}
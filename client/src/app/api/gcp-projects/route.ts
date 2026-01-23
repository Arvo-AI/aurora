import { NextRequest, NextResponse } from 'next/server'
import { auth } from '@/auth'

// Backend base URL (matches usage in other proxy routes)
const API_BASE_URL = process.env.BACKEND_URL

// ---------------------------------------------------------------------------
// GET /api/gcp-projects
// Calls backend → /api/gcp/sa-project-access (backend derives user from session)
// ---------------------------------------------------------------------------
export async function GET() {
  try {
    const session = await auth()

    if (!session?.userId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const userId = session.userId

    // Authenticated user - backend will derive user_id from session
    const backendResp = await fetch(
      `${API_BASE_URL}/api/gcp/sa-project-access`,
      {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          'X-User-Id': userId,
        },
        credentials: 'include',
      },
    )

    if (!backendResp.ok) {
      let errorData
      try {
        const jsonData = await backendResp.json()
        errorData = jsonData.error || jsonData.message || `Backend returned ${backendResp.status}`
      } catch {
        errorData = await backendResp.text() || `Backend returned ${backendResp.status}`
      }
      return NextResponse.json(
        { error: errorData },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (err) {
    console.error('Error fetching service-account project list:', err)
    return NextResponse.json(
      { error: 'Failed to fetch project list' },
      { status: 500 },
    )
  }
}

// ---------------------------------------------------------------------------
// POST /api/gcp-projects
// Sends updated list back to backend → /api/gcp/sa-project-access (backend derives user from session)
// Body: { projects: [{ projectId, enabled }] }
// ---------------------------------------------------------------------------
export async function POST(request: NextRequest) {
  try {
    const session = await auth()
    const body = await request.json()

    if (!session?.userId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const userId = session.userId

    // Authenticated user - backend will derive user_id from session
    const backendResp = await fetch(`${API_BASE_URL}/api/gcp/sa-project-access`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Id': userId,
      },
      credentials: 'include',
      body: JSON.stringify({ projects: body.projects }),
    })

    if (!backendResp.ok) {
      let errorData
      try {
        const jsonData = await backendResp.json()
        errorData = jsonData.error || jsonData.message || `Backend returned ${backendResp.status}`
      } catch {
        errorData = await backendResp.text() || `Backend returned ${backendResp.status}`
      }
      return NextResponse.json(
        { error: errorData },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (err) {
    console.error('Error updating SA project access:', err)
    return NextResponse.json(
      { error: 'Failed to update project access' },
      { status: 500 },
    )
  }
} 
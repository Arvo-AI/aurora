import { NextRequest, NextResponse } from 'next/server'
import { auth } from '@/auth'

const API_BASE_URL = process.env.BACKEND_URL

export async function GET(request: NextRequest) {
  try {
    const session = await auth()

    if (!session?.userId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }

    const userId = session.userId

    const backendResp = await fetch(
      `${API_BASE_URL}/user_tokens?user_id=${userId}`,
      {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          'X-User-ID': userId,
        },
        credentials: 'include',
      }
    )

    if (!backendResp.ok) {
      const text = await backendResp.text()
      console.error(`Backend user_tokens error: ${backendResp.status} - ${text}`)
      return NextResponse.json(
        { error: `Backend error: ${backendResp.status}` },
        { status: backendResp.status }
      )
    }

    const data = await backendResp.json()
    return NextResponse.json(data)
  } catch (err) {
    console.error('Error fetching user tokens:', err)
    return NextResponse.json(
      { error: 'Failed to fetch user tokens' },
      { status: 500 }
    )
  }
}

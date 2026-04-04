import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL || 'http://localhost:5080'

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser()
    if (authResult instanceof NextResponse) return authResult

    const { id } = await params
    const response = await fetch(`${API_BASE_URL}/api/skills/${id}`, {
      headers: { ...authResult.headers },
      cache: 'no-store',
    })

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/skills/[id]] GET error:', error instanceof Error ? error.message : 'Unknown')
    return NextResponse.json({ error: 'Failed to fetch skill' }, { status: 500 })
  }
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser()
    if (authResult instanceof NextResponse) return authResult

    const { id } = await params
    const body = await request.json()
    const response = await fetch(`${API_BASE_URL}/api/skills/${id}`, {
      method: 'PUT',
      headers: { ...authResult.headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/skills/[id]] PUT error:', error instanceof Error ? error.message : 'Unknown')
    return NextResponse.json({ error: 'Failed to update skill' }, { status: 500 })
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser()
    if (authResult instanceof NextResponse) return authResult

    const { id } = await params
    const response = await fetch(`${API_BASE_URL}/api/skills/${id}`, {
      method: 'DELETE',
      headers: { ...authResult.headers },
    })

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/skills/[id]] DELETE error:', error instanceof Error ? error.message : 'Unknown')
    return NextResponse.json({ error: 'Failed to delete skill' }, { status: 500 })
  }
}

import { NextRequest, NextResponse } from 'next/server'
import { env } from '@/lib/server-env'

export async function POST(request: NextRequest) {
  try {
    const body = await request.text()
    const url = `${env.BACKEND_URL}/api/auth/register`

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    }
    if (env.INTERNAL_API_SECRET) {
      headers['X-Internal-Secret'] = env.INTERNAL_API_SECRET
    }

    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 30_000)

    let response: Response
    try {
      response = await fetch(url, {
        method: 'POST',
        headers,
        body,
        signal: controller.signal,
      })
      clearTimeout(timeoutId)
    } catch (fetchErr: unknown) {
      clearTimeout(timeoutId)
      if (fetchErr instanceof Error && fetchErr.name === 'AbortError') {
        return NextResponse.json({ error: 'Request timeout' }, { status: 504 })
      }
      throw fetchErr
    }

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    console.error('[api/auth/register] Error:', error instanceof Error ? error.message : 'unknown')
    return NextResponse.json({ error: 'Registration failed' }, { status: 500 })
  }
}

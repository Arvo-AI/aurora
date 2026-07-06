import { NextRequest, NextResponse } from 'next/server'
import { env } from '@/lib/server-env'

export async function POST(request: NextRequest) {
  try {
    const body = await request.text()

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
      response = await fetch(`${env.BACKEND_URL}/api/auth/register`, {
        method: 'POST',
        headers,
        body,
        signal: controller.signal,
      })
      clearTimeout(timeoutId)
    } catch (fetchErr: unknown) {
      clearTimeout(timeoutId)
      if (fetchErr instanceof Error && fetchErr.name === 'AbortError') {
        return NextResponse.json({ error: 'Request timeout for register' }, { status: 504 })
      }
      throw fetchErr
    }

    if (!response.ok) {
      let errorMsg = 'Registration failed'
      try {
        const data = await response.json()
        if (data.error && typeof data.error === 'string') {
          errorMsg = data.error
        }
      } catch {
        // non-JSON error body — use generic message
      }
      return NextResponse.json(
        { error: errorMsg },
        { status: response.status },
      )
    }

    const data = await response.json()
    return NextResponse.json(data, { status: response.status })
  } catch (error) {
    const safeError = error instanceof Error ? { message: error.message, name: error.name } : {}
    console.error('[api/register] Error:', safeError)
    return NextResponse.json({ error: 'Failed to register' }, { status: 500 })
  }
}

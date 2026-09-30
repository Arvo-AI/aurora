import { NextRequest, NextResponse } from 'next/server'
import { env } from '@/lib/server-env'

// Unauthenticated by design: the caller is locked out of their account, so
// there is no session to forward. Cannot use forwardRequest (which requires an
// authenticated user), so the internal-secret call is made directly here.
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
      response = await fetch(`${env.BACKEND_URL}/api/auth/forgot-password`, {
        method: 'POST',
        headers,
        body,
        signal: controller.signal,
      })
      clearTimeout(timeoutId)
    } catch (fetchErr: unknown) {
      clearTimeout(timeoutId)
      if (fetchErr instanceof Error && fetchErr.name === 'AbortError') {
        return NextResponse.json({ error: 'Request timeout for forgot-password' }, { status: 504 })
      }
      throw fetchErr
    }

    // The backend answers 200 with the same message whether or not the account
    // exists; pass its body through untouched so we don't leak a difference.
    try {
      const data = await response.json()
      return NextResponse.json(data, { status: response.status })
    } catch {
      return NextResponse.json({ error: 'Unexpected response' }, { status: 502 })
    }
  } catch (error) {
    const safeError = error instanceof Error ? { message: error.message, name: error.name } : {}
    console.error('[api/forgot-password] Error:', safeError)
    return NextResponse.json({ error: 'Failed to request password reset' }, { status: 500 })
  }
}

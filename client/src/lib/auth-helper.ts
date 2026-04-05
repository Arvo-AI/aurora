import { NextResponse } from 'next/server'
import { auth } from '@/auth'

export interface AuthResult {
  userId: string
  orgId?: string
  headers: Record<string, string>
}

/**
 * Get authenticated user from Auth.js session
 */
export async function getAuthenticatedUser(): Promise<AuthResult | NextResponse> {  
  const session = await auth()
  
  if (!session?.userId) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
  }

  const headers: Record<string, string> = {
    'X-User-ID': session.userId,
  }

  if (session.orgId && session.orgId.trim() !== '') {
    headers['X-Org-ID'] = session.orgId
  }

  return {
    userId: session.userId,
    orgId: session.orgId,
    headers,
  }
}

/**
 * Make authenticated request with Auth.js user ID header
 */
export async function makeAuthenticatedRequest(
  url: string,
  options: RequestInit = {},
  additionalHeaders: Record<string, string> = {}
): Promise<Response> {
  const authResult = await getAuthenticatedUser()

  if (authResult instanceof NextResponse) {
    throw new Error('User not authenticated')
  }

  // Default 10s timeout unless caller provides their own signal
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 10_000)
  if (options.signal) {
    options.signal.addEventListener('abort', () => controller.abort())
  }

  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        ...options.headers,
        ...authResult.headers,
        ...additionalHeaders,
      }
    })
  } finally {
    clearTimeout(timeout)
  }
}

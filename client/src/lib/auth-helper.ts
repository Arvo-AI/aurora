import { NextResponse } from 'next/server'
import { auth } from '@/auth'

export interface AuthResult {
  userId: string
  orgId?: string
  headers: Record<string, string>
}

const INTERNAL_API_SECRET = process.env.INTERNAL_API_SECRET || ''

if (!INTERNAL_API_SECRET) {
  console.warn('[auth-helper] INTERNAL_API_SECRET not set — requests to Flask will fail if the server requires it')
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

  if (INTERNAL_API_SECRET) {
    headers['X-Internal-Secret'] = INTERNAL_API_SECRET
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

  // Default 30s timeout unless caller provides their own signal
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 30_000)
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

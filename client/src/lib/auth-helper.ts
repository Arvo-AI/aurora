import { NextResponse } from 'next/server'
import { auth } from '@/auth'

export interface AuthResult {
  userId: string
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

  return {
    userId: session.userId,
    headers: {
      'X-User-ID': session.userId,
    }
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

  return fetch(url, {
    ...options,
    headers: {
      ...options.headers,
      ...authResult.headers,
      ...additionalHeaders,
    }
  })
}

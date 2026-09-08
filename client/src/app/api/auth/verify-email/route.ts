import { NextRequest } from 'next/server'
import { forwardRequest } from '@/lib/backend-proxy'
import { getAuthenticatedUser } from '@/lib/auth-helper'

export async function POST(request: NextRequest) {
  const result = await forwardRequest(request, 'POST', '/api/auth/verify-email', 'verify-email')

  if (result.status === 200) {
    const authResult = await getAuthenticatedUser()
    if (!(authResult instanceof Response) && authResult.userId) {
      result.cookies.set('aurora-email-verified', authResult.userId, {
        httpOnly: true,
        secure: process.env.NODE_ENV === 'production',
        sameSite: 'lax',
        path: '/',
        maxAge: 120,
      })
    }
  }

  return result
}

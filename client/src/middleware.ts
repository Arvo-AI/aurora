import { auth } from "@/auth"
import { NextResponse } from "next/server"
import { ROLE_ADMIN } from "@/lib/roles"

// Public routes that don't require authentication
const publicRoutes = [
  "/sign-in",
  "/sign-up",
  "/change-password",
  "/terms",
  "/api/auth/callback",  // NextAuth callbacks
  "/api/auth/signin",     // NextAuth sign-in
  "/api/auth/signout",    // NextAuth sign-out
  "/api/auth/session",    // NextAuth session
  "/api/auth/providers",  // NextAuth providers
  "/api/auth/csrf",       // NextAuth CSRF
  "/api/auth/change-password", // Password change API
  "/api/auth/setup-org",  // Org setup for org-less users
  "/google-chat/events",  // Google Chat event POSTs (rewritten to backend)
  "/api/ping",            // Connection health check
]

// Routes that should redirect authenticated users away
const authRoutes = ["/sign-in", "/sign-up"]

export default auth((req) => {
  const { nextUrl } = req
  const isLoggedIn = !!req.auth?.user?.id
  
  const isPublicRoute = publicRoutes.some(route => 
    nextUrl.pathname.startsWith(route)
  )
  const isAuthRoute = authRoutes.some(route =>
    nextUrl.pathname.startsWith(route)
  )
  const isApiRoute = nextUrl.pathname.startsWith('/api/')
  const isAdminRoute = nextUrl.pathname.startsWith('/admin') || nextUrl.pathname.startsWith('/api/admin')
  const isChangePasswordRoute = nextUrl.pathname.startsWith('/change-password')
  const isSetupOrgRoute = nextUrl.pathname.startsWith('/setup-org')
  const isOrgSwitching = nextUrl.pathname.startsWith('/org/switching')

  // If user is logged in and tries to access auth pages, redirect to home
  if (isAuthRoute && isLoggedIn) {
    return NextResponse.redirect(new URL("/", nextUrl))
  }

  // Force password change: redirect to /change-password if flag is set
  if (isLoggedIn && req.auth?.user?.mustChangePassword && !isChangePasswordRoute && !isApiRoute) {
    return NextResponse.redirect(new URL("/change-password", nextUrl))
  }

  // Force org setup: redirect users without an org (or in Default Org) to create one
  const orgName = req.auth?.user?.orgName
  const needsOrg = !req.auth?.user?.orgId || (orgName && orgName.toLowerCase() === "default organization")
  if (isLoggedIn && needsOrg && !isSetupOrgRoute && !isOrgSwitching && !isChangePasswordRoute && !isApiRoute) {
    return NextResponse.redirect(new URL("/setup-org", nextUrl))
  }

  // If user is not logged in and tries to access protected route
  if (!isPublicRoute && !isLoggedIn) {
    // For API routes, return 401 JSON response instead of redirecting
    if (isApiRoute) {
      return NextResponse.json(
        { error: "Unauthorized" },
        { status: 401 }
      )
    }
    
    // For page routes, redirect to sign-in
    const callbackUrl = nextUrl.pathname + nextUrl.search
    const signInUrl = new URL("/sign-in", nextUrl)
    signInUrl.searchParams.set("callbackUrl", callbackUrl)
    return NextResponse.redirect(signInUrl)
  }

  // Gate admin routes to admin role only
  if (isAdminRoute && isLoggedIn) {
    const role = req.auth?.user?.role
    if (role !== ROLE_ADMIN) {
      if (isApiRoute) {
        return NextResponse.json({ error: "Forbidden" }, { status: 403 })
      }
      return NextResponse.redirect(new URL("/", nextUrl))
    }
  }

  const response = NextResponse.next()

  const backendOrigin = process.env.PUBLIC_API_URL || process.env.NEXT_PUBLIC_BACKEND_URL || ''
  const wsOrigin = process.env.PUBLIC_WS_URL || process.env.NEXT_PUBLIC_WEBSOCKET_URL || ''
  const connectSrc = [
    "'self'",
    backendOrigin,
    wsOrigin,
    backendOrigin ? backendOrigin.replace(/^http/, 'ws') : '',
  ].filter(Boolean).join(' ')

  response.headers.set('Content-Security-Policy', [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "font-src 'self' data:",
    `connect-src ${connectSrc}`,
    "frame-ancestors 'none'",
  ].join('; '))

  return response
})

export const config = {
  matcher: ['/((?!.+\\.[\\w]+$|_next).*)', '/', '/(api|trpc)(.*)'],
};

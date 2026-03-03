import { auth } from "@/auth"
import { NextResponse } from "next/server"
import { ROLE_ADMIN } from "@/lib/roles"

// Public routes that don't require authentication
const publicRoutes = [
  "/sign-in",
  "/sign-up",
  "/terms",
  "/api/auth/callback",  // NextAuth callbacks
  "/api/auth/signin",     // NextAuth sign-in
  "/api/auth/signout",    // NextAuth sign-out
  "/api/auth/session",    // NextAuth session
  "/api/auth/providers",  // NextAuth providers
  "/api/auth/csrf",       // NextAuth CSRF
]

// Routes that should redirect authenticated users away
const authRoutes = ["/sign-in", "/sign-up"]

export default auth((req) => {
  const { nextUrl } = req
  const isLoggedIn = !!req.auth
  
  const isPublicRoute = publicRoutes.some(route => 
    nextUrl.pathname.startsWith(route)
  )
  const isAuthRoute = authRoutes.some(route =>
    nextUrl.pathname.startsWith(route)
  )
  const isApiRoute = nextUrl.pathname.startsWith('/api/')
  const isAdminRoute = nextUrl.pathname.startsWith('/admin') || nextUrl.pathname.startsWith('/api/admin')

  // If user is logged in and tries to access auth pages, redirect to home
  if (isAuthRoute && isLoggedIn) {
    return NextResponse.redirect(new URL("/", nextUrl))
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

  return NextResponse.next()
})

export const config = {
  matcher: ['/((?!.+\\.[\\w]+$|_next).*)', '/', '/(api|trpc)(.*)'],
};
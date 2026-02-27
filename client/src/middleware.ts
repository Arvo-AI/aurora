import { auth } from "@/auth"
import { NextResponse } from "next/server"

// Public routes that don't require authentication
const publicRoutes = [
  "/sign-in",
  "/sign-up",
  "/terms",
  "/api/auth/register",   // Flask registration endpoint
  "/api/auth/login",      // Flask login endpoint
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

  return NextResponse.next()
})

export const config = {
  matcher: ['/((?!.+\\.[\\w]+$|_next).*)', '/', '/(api|trpc)(.*)'],
};
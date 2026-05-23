/**
 * Clerk-based auth configuration for SaaS mode.
 *
 * When NEXT_PUBLIC_SAAS_MODE=true, this module is used instead of the
 * default credentials-based auth.ts. Clerk handles:
 * - Google, GitHub, Magic Link, SSO sign-in
 * - Session management with short-lived JWTs
 * - User/org lifecycle (synced to our DB via webhooks)
 *
 * The Clerk session JWT is decoded server-side to extract the user ID,
 * which is then passed as X-User-ID to the Flask backend (same contract
 * as the credentials flow).
 */

import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

const isPublicRoute = createRouteMatcher([
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/api/webhooks/(.*)",
  "/api/ping",
  "/terms",
]);

export default clerkMiddleware(async (auth, req) => {
  if (isPublicRoute(req)) {
    return NextResponse.next();
  }

  const { userId } = await auth();

  if (!userId) {
    const isApi = req.nextUrl.pathname.startsWith("/api/");
    if (isApi) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }
    const signInUrl = new URL("/sign-in", req.url);
    signInUrl.searchParams.set("redirect_url", req.nextUrl.pathname);
    return NextResponse.redirect(signInUrl);
  }

  return NextResponse.next();
});

export const config = {
  matcher: ["/((?!.+\\.[\\w]+$|_next).*)", "/", "/(api|trpc)(.*)"],
};

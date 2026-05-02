import NextAuth from "next-auth"
import Credentials from "next-auth/providers/credentials"

const ROLE_REVALIDATE_SECONDS = 60 // re-check role/org every 60 seconds

type RefreshResult = {
  role: string
  orgId: string | null
  orgName: string | null
  mustChangePassword: boolean
} | null | "not_found"

// Deduplicate concurrent refresh calls — all middleware requests share one
// in-flight fetch instead of each independently hitting a stale connection.
let inflightRefresh: Promise<RefreshResult> | null = null

async function refreshUserFromBackend(userId: string): Promise<RefreshResult> {
  if (inflightRefresh) return inflightRefresh

  inflightRefresh = doRefreshUserFromBackend(userId).finally(() => {
    inflightRefresh = null
  })

  return inflightRefresh
}

async function doRefreshUserFromBackend(userId: string): Promise<RefreshResult> {
  const backendUrl = process.env.BACKEND_URL
  if (!backendUrl) return null

  try {
    const controller = new AbortController()
    const abortTimeout = setTimeout(() => controller.abort(), 5000)

    // Promise.race guarantees we return within 3s at the JS level, even if
    // the runtime's native TCP layer is blocked retransmitting on a stale
    // socket (which ignores AbortController for up to ~20s).
    const result = await Promise.race<RefreshResult>([
      (async () => {
        const res = await fetch(`${backendUrl}/api/auth/me`, {
          headers: { "X-User-ID": userId },
          cache: "no-store",
          signal: controller.signal,
        })
        clearTimeout(abortTimeout)
        if (res.status === 404) return "not_found" as const
        if (!res.ok) return null
        return await res.json()
      })(),
      new Promise<null>(resolve => setTimeout(() => resolve(null), 3000)),
    ])

    clearTimeout(abortTimeout)
    return result
  } catch (err) {
    // Intentionally return null on failure so the JWT keeps its current
    // values and the user isn't logged out by a transient backend error.
    console.error("Failed to refresh user from backend:", err)
    return null
  }
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  // trustHost: true in development, false in production
  // In production, Auth.js will use FRONTEND_URL or infer from request headers
  trustHost: true,
  secret: process.env.AUTH_SECRET,
  providers: [
    Credentials({
      name: "credentials",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" }
      },
      authorize: async (credentials) => {
        if (!credentials?.email || !credentials?.password) {
          return null
        }

        const backendUrl = process.env.BACKEND_URL
        if (!backendUrl) {
          console.error("BACKEND_URL environment variable is not set")
          return null
        }

        const loginController = new AbortController()
        const loginTimeout = setTimeout(() => loginController.abort(), 10000)
        const response = await fetch(`${backendUrl}/api/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: credentials.email,
            password: credentials.password
          }),
          signal: loginController.signal,
        })
        clearTimeout(loginTimeout)
        
        if (!response.ok) {
          console.error("Login failed:", response.status)
          return null
        }
        
        const user = await response.json()
        return user // { id, email, name, role, orgId, orgName }
      }
    })
  ],
  session: {
    strategy: "jwt",
    maxAge: 7 * 24 * 60 * 60 // 7 days
  },
  pages: {
    signIn: "/sign-in",
    error: "/sign-in"
  },
  callbacks: {
    async jwt({ token, user, trigger }) {
      if (user) {
        token.id = user.id
        token.email = user.email
        token.name = user.name
        token.role = user.role
        token.orgId = user.orgId
        token.orgName = user.orgName
        token.mustChangePassword = user.mustChangePassword
        token.lastRefreshedAt = Math.floor(Date.now() / 1000)
        return token
      }

      const lastRefreshed = (token.lastRefreshedAt as number) || 0
      const now = Math.floor(Date.now() / 1000)

      if (trigger === "update" || now - lastRefreshed > ROLE_REVALIDATE_SECONDS) {
        const fresh = await refreshUserFromBackend(token.id as string)
        if (fresh === "not_found") {
          // User no longer exists in DB (stale session after DB reset).
          // Wipe the token so the session callback produces an empty
          // session, which middleware treats as logged-out.
          token.id = undefined
          token.email = undefined
          token.name = undefined
          token.lastRefreshedAt = now
          return token
        }
        if (fresh) {
          token.role = fresh.role
          token.orgId = fresh.orgId
          token.orgName = fresh.orgName
          token.mustChangePassword = fresh.mustChangePassword
          token.lastRefreshedAt = now
        }
        // On failure (fresh === null, typically the 3s timeout after a busy
        // backend write burst), do NOT advance lastRefreshedAt — otherwise
        // the next 60s of GET /api/auth/session calls short-circuit on the
        // stale token, leaving the user with the old org_id in their cookie
        // until the time-based check re-fires. This was the root cause of
        // DEV-1100: the post-invite-accept refresh would lose the race once
        // and then never re-attempt within the /org/switching polling window.
      }

      return token
    },
    session({ session, token }) {
      if (token) {
        session.userId = token.id as string
        session.orgId = (token.orgId as string) ?? undefined
        if (session.user) {
          session.user.id = token.id as string
          session.user.email = token.email as string
          session.user.name = token.name as string
          session.user.role = token.role as string
          session.user.orgId = (token.orgId as string) ?? undefined
          session.user.orgName = (token.orgName as string) ?? undefined
          session.user.mustChangePassword = token.mustChangePassword as boolean
        }
      }
      return session
    }
  }
})

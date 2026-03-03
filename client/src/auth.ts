import NextAuth from "next-auth"
import Credentials from "next-auth/providers/credentials"

const ROLE_REVALIDATE_SECONDS = 5 * 60 // re-check role/org every 5 minutes

async function refreshUserFromBackend(userId: string): Promise<{
  role: string
  orgId: string | null
  orgName: string | null
} | null> {
  const backendUrl = process.env.BACKEND_URL
  if (!backendUrl) return null

  try {
    const res = await fetch(`${backendUrl}/api/auth/me`, {
      headers: { "X-User-ID": userId },
      cache: "no-store",
    })
    if (!res.ok) return null
    return await res.json()
  } catch (err) {
    console.error("Failed to refresh user from backend:", err)
    return null
  }
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  // trustHost: true in development, false in production
  // In production, Auth.js will use FRONTEND_URL or infer from request headers
  trustHost: process.env.NODE_ENV !== 'production',
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

        const response = await fetch(`${backendUrl}/api/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: credentials.email,
            password: credentials.password
          })
        })
        
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
    async jwt({ token, user }) {
      if (user) {
        token.id = user.id
        token.email = user.email
        token.name = user.name
        token.role = user.role
        token.orgId = user.orgId
        token.orgName = user.orgName
        token.lastRefreshedAt = Math.floor(Date.now() / 1000)
        return token
      }

      const lastRefreshed = (token.lastRefreshedAt as number) || 0
      const now = Math.floor(Date.now() / 1000)

      if (now - lastRefreshed > ROLE_REVALIDATE_SECONDS) {
        const fresh = await refreshUserFromBackend(token.id as string)
        if (fresh) {
          token.role = fresh.role
          token.orgId = fresh.orgId
          token.orgName = fresh.orgName
        }
        token.lastRefreshedAt = now
      }

      return token
    },
    session({ session, token }) {
      if (token) {
        session.userId = token.id as string
        session.orgId = token.orgId as string
        if (session.user) {
          session.user.id = token.id as string
          session.user.email = token.email as string
          session.user.name = token.name as string
          session.user.role = token.role as string
          session.user.orgId = token.orgId as string
          session.user.orgName = token.orgName as string
        }
      }
      return session
    }
  }
})

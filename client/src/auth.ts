import NextAuth from "next-auth"
import Credentials from "next-auth/providers/credentials"

export const { handlers, signIn, signOut, auth } = NextAuth({
  // trustHost: true in development, false in production
  // In production, Auth.js will use FRONTEND_URL or infer from request headers
  trustHost: process.env.NODE_ENV !== 'prod',
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
        return user // { id, email, name }
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
    jwt({ token, user }) {
      if (user) {
        token.id = user.id
        token.email = user.email
        token.name = user.name
      }
      return token
    },
    session({ session, token }) {
      if (token) {
        session.userId = token.id as string
        if (session.user) {
          session.user.id = token.id as string
          session.user.email = token.email as string
          session.user.name = token.name as string
        }
      }
      return session
    }
  }
})

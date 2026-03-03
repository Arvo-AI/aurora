import { DefaultSession } from "next-auth"

declare module "next-auth" {
  interface Session {
    userId: string
    orgId?: string
    user: {
      id: string
      email: string
      name?: string
      role?: string
      orgId?: string
      orgName?: string
    } & DefaultSession["user"]
  }

  interface User {
    id: string
    email: string
    name?: string
    role?: string
    orgId?: string
    orgName?: string
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    id: string
    email: string
    name?: string
    role?: string
    orgId?: string
    orgName?: string
  }
}

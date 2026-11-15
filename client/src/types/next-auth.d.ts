import { DefaultSession } from "next-auth"

export type UserRole = "admin" | "editor" | "viewer";

declare module "next-auth" {
  interface Session {
    userId: string
    orgId?: string
    user: {
      id: string
      email: string
      name?: string
      role?: UserRole
      orgId?: string
      orgName?: string
      mustChangePassword?: boolean
      emailVerified?: boolean
      isGithubProvisioned?: boolean
    } & DefaultSession["user"]
  }

  interface User {
    id: string
    email: string
    name?: string
    role?: UserRole
    orgId?: string
    orgName?: string
    mustChangePassword?: boolean
    emailVerified?: boolean
    isGithubProvisioned?: boolean
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    id: string
    email: string
    name?: string
    role?: UserRole
    orgId?: string
    orgName?: string
    mustChangePassword?: boolean
    emailVerified?: boolean
    isGithubProvisioned?: boolean
    lastRefreshedAt?: number
  }
}

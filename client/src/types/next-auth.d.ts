import { DefaultSession } from "next-auth"

declare module "next-auth" {
  interface Session {
    userId: string
    user: {
      id: string
      email: string
      name?: string
      role?: string
    } & DefaultSession["user"]
  }

  interface User {
    id: string
    email: string
    name?: string
    role?: string
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    id: string
    email: string
    name?: string
    role?: string
  }
}

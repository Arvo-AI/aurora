"use client"

import { usePathname } from "next/navigation"
import ClientShell from "./ClientShell"

export default function ConditionalShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()

  const isAuthPage = pathname?.startsWith("/sign-in") || pathname?.startsWith("/sign-up") || pathname?.startsWith("/change-password") || pathname?.startsWith("/setup-org")
  const isLegalPage = pathname?.startsWith("/terms")

  if (isAuthPage || isLegalPage) {
    return <>{children}</>
  }

  return <ClientShell>{children}</ClientShell>
}

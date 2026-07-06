"use client"

import { usePathname } from "next/navigation"
import ClientShell from "./ClientShell"

export default function ConditionalShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()

  const isAuthPage = pathname?.startsWith("/sign-in") || pathname?.startsWith("/setup-org") || pathname?.startsWith("/onboarding")
  const isLegalPage = pathname?.startsWith("/terms")
  const isTransitionPage = pathname?.startsWith("/org/switching")

  if (isAuthPage || isLegalPage || isTransitionPage) {
    return <>{children}</>
  }

  return <ClientShell>{children}</ClientShell>
}

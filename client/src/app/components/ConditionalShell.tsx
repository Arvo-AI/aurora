"use client"

import { usePathname } from "next/navigation"
import ClientShell from "./ClientShell"

export default function ConditionalShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  
  // Don't wrap auth pages and legal pages in ClientShell (which has AppLayout, navigation, etc.)
  const isAuthPage = pathname?.startsWith("/sign-in") || pathname?.startsWith("/sign-up")
  const isLegalPage = pathname?.startsWith("/terms")
  
  if (isAuthPage || isLegalPage) {
    return <>{children}</>
  }
  
  return <ClientShell>{children}</ClientShell>
}

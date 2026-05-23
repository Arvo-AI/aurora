"use client";

import { SessionProvider } from "next-auth/react";
import { isSaasMode } from "@/lib/feature-flags";

let ClerkProvider: React.ComponentType<{ children: React.ReactNode }> | null = null;

if (isSaasMode()) {
  try {
    // Dynamic import at module level for tree-shaking in OSS mode
    const clerk = require("@clerk/nextjs");
    ClerkProvider = clerk.ClerkProvider;
  } catch {
    // @clerk/nextjs not installed — fall back to session provider
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  if (ClerkProvider) {
    return <ClerkProvider>{children}</ClerkProvider>;
  }

  return (
    <SessionProvider refetchOnWindowFocus={true}>
      {children}
    </SessionProvider>
  );
}

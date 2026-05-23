"use client";

import { SessionProvider } from "next-auth/react";
import { isSaasMode } from "@/lib/feature-flags";
import dynamic from "next/dynamic";

const ClerkProviderWrapper = dynamic(
  () => import("@clerk/nextjs").then((mod) => {
    const { ClerkProvider } = mod;
    return function Wrapper({ children }: { children: React.ReactNode }) {
      return <ClerkProvider>{children}</ClerkProvider>;
    };
  }),
  { ssr: false }
);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  if (isSaasMode()) {
    return <ClerkProviderWrapper>{children}</ClerkProviderWrapper>;
  }

  return (
    <SessionProvider refetchOnWindowFocus={true}>
      {children}
    </SessionProvider>
  );
}

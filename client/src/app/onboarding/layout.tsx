"use client"

import AuroraShader from "@/app/components/AuroraShader"
import { OnboardingProvider } from "./components/OnboardingContext"
import { useDarkPageBackground } from "@/hooks/useDarkPageBackground"

export default function OnboardingLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  useDarkPageBackground()

  return (
    <OnboardingProvider>
      <div className="h-screen bg-[#0a0a0a] relative overflow-hidden">
        <div className="fixed inset-0">
          <AuroraShader className="absolute inset-0 w-full h-full blur-[8px]" />
          <div className="absolute inset-0 bg-black/40" />
          <div
            className="absolute inset-0"
            style={{
              background:
                "linear-gradient(to bottom, transparent 0%, transparent 30%, rgba(0,0,0,0.4) 60%, rgba(0,0,0,0.75) 100%)",
            }}
          />
        </div>
        <div className="relative z-10 h-screen overflow-hidden">
          {children}
        </div>
      </div>
    </OnboardingProvider>
  )
}

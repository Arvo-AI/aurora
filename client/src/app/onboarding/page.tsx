"use client"

import { Suspense } from "react"
import SetupHome from "@/app/onboarding/components/SetupHome"

export default function OnboardingPage() {
  return (
    <Suspense fallback={null}>
      <SetupHome />
    </Suspense>
  )
}

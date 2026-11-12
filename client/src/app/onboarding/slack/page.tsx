"use client"

import { Suspense } from "react"
import SetupHome from "../components/SetupHome"

// Kept as its own URL: Slack's welcome DM and App Home link here.
export default function SlackOnboardingPage() {
  return (
    <Suspense fallback={null}>
      <SetupHome via="slack" />
    </Suspense>
  )
}

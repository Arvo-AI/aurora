"use client"

import { useEffect, useState } from "react"

// The person from the Aurora team looking after this account during setup.
// Hosted only: self-hosted installs answer with guide = null.
export interface SetupGuide {
  name: string
  first_name: string
  title?: string
  avatar_url?: string
  booking_url?: string
  email?: string
}

export interface SetupJourney {
  guide: SetupGuide | null
  // How this account signed up: "github", "slack", "password", ...
  via: string | null
  first_name: string | null
}

let cached: Promise<SetupJourney> | null = null

function load(): Promise<SetupJourney> {
  cached ??= fetch("/api/onboarding/journey", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => ({
      guide: d?.guide ?? null,
      via: d?.via ?? null,
      first_name: d?.first_name ?? null,
    }))
    .catch(() => {
      cached = null
      return { guide: null, via: null, first_name: null }
    })
  return cached
}

export function useSetupJourney(): SetupJourney | null {
  const [journey, setJourney] = useState<SetupJourney | null>(null)
  useEffect(() => {
    let alive = true
    void load().then((j) => { if (alive) setJourney(j) })
    return () => { alive = false }
  }, [])
  return journey
}

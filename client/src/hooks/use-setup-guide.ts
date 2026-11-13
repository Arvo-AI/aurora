"use client"

import { useEffect, useState } from "react"
import { useSession } from "next-auth/react"

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
  // Whose team the guide is on ("Aurora" unless a self-hosted install says otherwise).
  team: string
}

const EMPTY: SetupJourney = { guide: null, via: null, first_name: null, team: "Aurora" }

async function load(): Promise<SetupJourney> {
  try {
    const r = await fetch("/api/onboarding/journey", { cache: "no-store" })
    if (!r.ok) return EMPTY
    const d = await r.json()
    return { guide: d?.guide ?? null, via: d?.via ?? null, first_name: d?.first_name ?? null, team: d?.team || "Aurora" }
  } catch {
    return EMPTY
  }
}

// Asked once per mount, and again when someone signs in or out, so nothing
// from one account is ever shown to another. The server decides whose
// journey it is from the session; the client never names an account.
// (Switching accounts or orgs reloads the page, which remounts this.)
export function useSetupJourney(): SetupJourney | null {
  const { status } = useSession()
  const [journey, setJourney] = useState<SetupJourney | null>(null)
  useEffect(() => {
    setJourney(null)
    if (status !== "authenticated") return
    let alive = true
    void load().then((j) => { if (alive) setJourney(j) })
    return () => { alive = false }
  }, [status])
  return journey
}

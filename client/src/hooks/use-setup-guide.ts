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
}

const EMPTY: SetupJourney = { guide: null, via: null, first_name: null }

// One request per signed-in account per page load. Keyed by account so a
// sign-in as someone else in the same tab never shows the previous guide.
const cache = new Map<string, Promise<SetupJourney>>()

function load(accountKey: string): Promise<SetupJourney> {
  const hit = cache.get(accountKey)
  if (hit) return hit
  const pending = fetch("/api/onboarding/journey", { cache: "no-store" })
    .then(async (r) => {
      if (!r.ok) throw new Error(`journey ${r.status}`)
      const d = await r.json()
      return { guide: d?.guide ?? null, via: d?.via ?? null, first_name: d?.first_name ?? null }
    })
    .catch(() => {
      // A failure isn't an answer: let the next mount ask again.
      cache.delete(accountKey)
      return EMPTY
    })
  cache.set(accountKey, pending)
  return pending
}

export function useSetupJourney(): SetupJourney | null {
  const { data: session, status } = useSession()
  const accountKey = session?.user?.id ?? ""
  const [journey, setJourney] = useState<SetupJourney | null>(null)
  useEffect(() => {
    if (status !== "authenticated" || !accountKey) return
    let alive = true
    void load(accountKey).then((j) => { if (alive) setJourney(j) })
    return () => { alive = false }
  }, [accountKey, status])
  return journey
}

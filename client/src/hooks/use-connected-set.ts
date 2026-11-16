"use client"

import { useCallback, useEffect, useRef, useState } from "react"

// Each status call runs live checks against the connected providers, so
// focus and tab switches refresh at most this often.
const FOCUS_MIN_GAP_MS = 15_000

// Connected provider ids from the live status checks the Connectors page uses.
// `connected` is null until the first answer, so callers can tell "loading"
// from "nothing"; `failed` is true while no answer has come back at all.
// Polls only while the tab is visible, and again when it regains focus, so
// finishing a connection in another tab shows up here. `enabled: false`
// makes no calls at all. Switching account or org reloads the page, which
// starts this from scratch; the client never names an account.
export function useConnectedSet(pollMs: number | null = 5000, enabled = true) {
  const [connected, setConnected] = useState<Set<string> | null>(null)
  const [failed, setFailed] = useState(false)
  const inFlight = useRef(false)
  const lastAt = useRef(0)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    lastAt.current = Date.now()
    try {
      const res = await fetch("/api/connectors/status", { cache: "no-store" })
      if (!res.ok) throw new Error(`status ${res.status}`)
      const data = await res.json()
      const ids = Object.entries((data?.connectors ?? {}) as Record<string, { connected?: boolean }>)
        .filter(([, v]) => v?.connected)
        .map(([k]) => k)
      setFailed(false)
      setConnected((prev) => {
        // Keep identity when nothing changed so effects keyed on it stay quiet.
        if (prev?.size === ids.length && ids.every((id) => prev?.has(id))) return prev
        return new Set(ids)
      })
    } catch {
      // Keep the last known state; the next poll tries again.
      setFailed(true)
    } finally {
      inFlight.current = false
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    void refresh()
    const tick = () => { if (document.visibilityState === "visible") void refresh() }
    const onFocus = () => { if (Date.now() - lastAt.current >= FOCUS_MIN_GAP_MS) tick() }
    const id = pollMs ? globalThis.setInterval(tick, pollMs) : undefined
    globalThis.addEventListener("focus", onFocus)
    document.addEventListener("visibilitychange", onFocus)
    return () => {
      if (id) globalThis.clearInterval(id)
      globalThis.removeEventListener("focus", onFocus)
      document.removeEventListener("visibilitychange", onFocus)
    }
  }, [refresh, pollMs, enabled])

  return { connected, failed, refresh }
}

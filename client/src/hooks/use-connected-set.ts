"use client"

import { useCallback, useEffect, useRef, useState } from "react"

// Connected provider ids from the live status checks the Connectors page uses.
// `connected` is null until the first answer, so callers can tell "loading"
// from "nothing"; `failed` is true while no answer has come back at all.
// Polls only while the tab is visible, and again whenever it regains focus,
// so finishing a connection in another tab shows up here.
export function useConnectedSet(pollMs: number | null = 5000) {
  const [connected, setConnected] = useState<Set<string> | null>(null)
  const [failed, setFailed] = useState(false)
  const inFlight = useRef(false)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
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
    void refresh()
    const tick = () => { if (document.visibilityState === "visible") void refresh() }
    const id = pollMs ? globalThis.setInterval(tick, pollMs) : undefined
    globalThis.addEventListener("focus", tick)
    document.addEventListener("visibilitychange", tick)
    return () => {
      if (id) globalThis.clearInterval(id)
      globalThis.removeEventListener("focus", tick)
      document.removeEventListener("visibilitychange", tick)
    }
  }, [refresh, pollMs])

  return { connected, failed, refresh }
}

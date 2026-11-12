"use client"

import Image from "next/image"
import { useEffect, useState } from "react"
import { AnimatePresence, motion } from "framer-motion"

// Slack-specific buttons for the setup page: open the workspace, play the
// demo incident, or retry an install that couldn't create its channel.

const GREEN = "#2eb67d"

export type SlackStatus = {
  connected: boolean
  team_name?: string
  team_id?: string
  team_url?: string
  app_id?: string
  incidents_channel_name?: string
}

export function slackDeepLink(s: SlackStatus | null): string {
  if (s?.app_id && s.team_id) return `https://slack.com/app_redirect?app=${encodeURIComponent(s.app_id)}&team=${encodeURIComponent(s.team_id)}`
  if (s?.team_url) return s.team_url
  return "https://app.slack.com/client"
}

// Workspace details, fetched only when Slack is how they came in or is connected.
export function useSlackStatus(relevant: boolean, refreshKey: unknown): SlackStatus | null {
  const [slack, setSlack] = useState<SlackStatus | null>(null)
  useEffect(() => {
    if (!relevant) return
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch("/api/slack", { cache: "no-store" })
        const data = res.ok ? await res.json() : { connected: false }
        if (!cancelled) setSlack(data)
      } catch {
        if (!cancelled) setSlack({ connected: false })
      }
    })()
    return () => { cancelled = true }
  }, [relevant, refreshKey])
  return slack
}

const BUTTON = "inline-flex items-center gap-2.5 rounded-lg px-5 py-2.5 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2"
const QUIET = `${BUTTON} border border-white/15 bg-white/[0.04] text-white hover:bg-white/[0.08] focus-visible:ring-white/30`

function RetrySlack() {
  const [retrying, setRetrying] = useState(false)
  const retry = async () => {
    setRetrying(true)
    try {
      const res = await fetch("/api/slack", { method: "POST" })
      const data = await res.json()
      if (data?.oauth_url) { globalThis.location.href = data.oauth_url; return }
    } catch { /* re-enable the button */ }
    setRetrying(false)
  }
  return (
    <button type="button" onClick={retry} disabled={retrying} className={`${BUTTON} bg-white text-black hover:bg-white/90 focus-visible:ring-white/40 focus-visible:ring-offset-2 focus-visible:ring-offset-black disabled:opacity-60`}>
      <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
      {retrying ? "Opening Slack…" : "Try Slack again"}
    </button>
  )
}

type DemoState = "idle" | "sending" | "sent" | "error"

function DemoLabel({ state }: Readonly<{ state: DemoState }>) {
  if (state === "sending") return <>Starting the demo…</>
  if (state === "error") return <>Couldn&apos;t start it. Try again</>
  if (state === "sent") return <><span className="h-1.5 w-1.5 rounded-full" style={{ background: GREEN }} />Playing in your Aurora DM</>
  return <>Watch a demo incident in Slack</>
}

function DemoButton({ reduce }: Readonly<{ reduce: boolean }>) {
  const [demo, setDemo] = useState<DemoState>("idle")
  const run = async () => {
    setDemo("sending")
    try {
      const res = await fetch("/api/slack/demo", { method: "POST" })
      setDemo(res.ok ? "sent" : "error")
    } catch {
      setDemo("error")
    }
  }
  return (
    <button type="button" onClick={run} disabled={demo === "sending" || demo === "sent"} className={`${QUIET} gap-2 disabled:cursor-default`}>
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={demo}
          initial={reduce ? false : { opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={reduce ? { opacity: 0 } : { opacity: 0, y: -4 }}
          transition={{ duration: 0.18 }}
          className="inline-flex items-center gap-2"
        >
          <DemoLabel state={demo} />
        </motion.span>
      </AnimatePresence>
    </button>
  )
}

export default function SlackActions({
  slack, failed, on, reduce,
}: Readonly<{ slack: SlackStatus | null; failed: boolean; on: boolean; reduce: boolean }>) {
  if (failed) return <RetrySlack />
  if (!on) return null
  return (
    <>
      <a href={slackDeepLink(slack)} target="_blank" rel="noopener noreferrer" className={QUIET}>
        <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
        {slack?.team_name ? `Open ${slack.team_name}` : "Open Slack"}
      </a>
      <DemoButton reduce={reduce} />
    </>
  )
}

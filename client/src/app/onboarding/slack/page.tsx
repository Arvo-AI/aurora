"use client"

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react"
import Image from "next/image"
import Link from "next/link"
import { useSearchParams } from "next/navigation"
import { useSession } from "next-auth/react"
import { AnimatePresence, motion, useReducedMotion } from "framer-motion"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import { ROLE_VIEWER } from "@/lib/roles"

// Landing page after the website's "Add to Slack" (and "Sign in with Slack"
// for teammates). The one orchestrated moment is the link animation at the
// top; everything after it only moves in answer to something changing.

const ALERT_IDS = ["pagerduty", "datadog", "grafana", "incidentio", "opsgenie", "newrelic"]
const ROOT_CAUSE_IDS = ["aws", "gcp", "azure", "kubectl", "github", "gitlab"]
const ALL_ALERT_IDS = new Set([...ALERT_IDS, "servicenow", "bigpanda", "dynatrace", "sentry", "splunk", "elastic", "coroot", "netdata", "cloudwatch", "thousandeyes"])
const ALL_ROOT_CAUSE_IDS = new Set([...ROOT_CAUSE_IDS, "onprem", "bitbucket", "cloudflare", "flyio", "ovh", "scaleway"])

const GREEN = "#2eb67d"
const EASE = [0.22, 1, 0.36, 1] as const
const POLL_MS = 5000

type SlackStatus = {
  connected: boolean
  team_name?: string
  team_id?: string
  team_url?: string
  app_id?: string
  incidents_channel_name?: string
}

type StepId = "slack" | "alerts" | "root_cause"

function slackDeepLink(s: SlackStatus | null): string {
  if (s?.app_id && s.team_id) return `https://slack.com/app_redirect?app=${encodeURIComponent(s.app_id)}&team=${encodeURIComponent(s.team_id)}`
  if (s?.team_url) return s.team_url
  return "https://app.slack.com/client"
}

function useConnectorStatus() {
  const [connected, setConnected] = useState<Set<string> | null>(null)
  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/connectors/status", { cache: "no-store" })
      if (!res.ok) return
      const data = await res.json()
      const ids = Object.entries((data?.connectors ?? {}) as Record<string, { connected?: boolean }>)
        .filter(([, v]) => v?.connected)
        .map(([k]) => k)
      setConnected(new Set(ids))
    } catch {
      // Keep the last known state; the next poll tries again.
    }
  }, [])
  useEffect(() => {
    void refresh()
    // Someone connecting PagerDuty in another tab should see this page tick over.
    const tick = () => { if (document.visibilityState === "visible") void refresh() }
    const id = globalThis.setInterval(tick, POLL_MS)
    globalThis.addEventListener("focus", tick)
    return () => { globalThis.clearInterval(id); globalThis.removeEventListener("focus", tick) }
  }, [refresh])
  return connected
}

/* ── The link: Aurora and Slack, joined by a line of aurora light ── */

function LinkMark({ reduce, failed }: Readonly<{ reduce: boolean; failed: boolean }>) {
  const tile = "relative h-16 w-16 sm:h-[72px] sm:w-[72px] rounded-2xl flex items-center justify-center shadow-[0_10px_40px_-10px_rgba(0,0,0,0.8)]"
  const t = (delay: number, duration = 0.5) => (reduce ? { duration: 0 } : { delay, duration, ease: EASE })
  return (
    <div className="flex items-center" aria-hidden="true">
      <motion.div
        className={`${tile} overflow-hidden border border-white/10 bg-black`}
        initial={reduce ? false : { opacity: 0, x: -14, scale: 0.92 }}
        animate={{ opacity: 1, x: 0, scale: 1 }}
        transition={t(0.05)}
      >
        <Image src="/arvologo.png" alt="" width={72} height={72} className="h-full w-full object-cover" priority />
      </motion.div>

      <div className="relative mx-3 h-[2px] w-20 sm:w-32">
        <div className="absolute inset-0 rounded-full bg-white/[0.06]" />
        <motion.div
          className="absolute inset-0 origin-left rounded-full"
          style={{
            background: failed
              ? "linear-gradient(90deg, #26e680 0%, #1a99cc 45%, rgba(255,255,255,0.08) 70%)"
              : "linear-gradient(90deg, #26e680 0%, #1a99cc 50%, #9926b3 100%)",
            boxShadow: "0 0 18px rgba(38,230,128,0.35)",
          }}
          initial={reduce ? false : { scaleX: 0 }}
          animate={{ scaleX: 1 }}
          transition={t(0.45, 0.65)}
        />
        {!reduce && !failed && (
          <motion.div
            className="absolute top-1/2 h-2 w-2 -translate-y-1/2 rounded-full bg-white"
            style={{ boxShadow: "0 0 12px 3px rgba(255,255,255,0.7)" }}
            initial={{ left: "0%", opacity: 0 }}
            animate={{ left: ["0%", "100%"], opacity: [0, 1, 1, 0] }}
            transition={{ delay: 1.05, duration: 0.7, ease: "easeInOut", times: [0, 0.15, 0.85, 1] }}
          />
        )}
      </div>

      <motion.div
        className={`${tile} bg-white`}
        initial={reduce ? false : { opacity: 0, x: 14, scale: 0.92 }}
        animate={{ opacity: 1, x: 0, scale: 1 }}
        transition={t(0.05)}
      >
        <Image src="/slack.png" alt="" width={40} height={40} className="h-9 w-9 sm:h-10 sm:w-10 object-contain" priority />
        {!reduce && !failed && (
          <motion.span
            className="absolute inset-0 rounded-2xl"
            style={{ boxShadow: `0 0 0 2px ${GREEN}` }}
            initial={{ opacity: 0, scale: 1 }}
            animate={{ opacity: [0, 1, 0], scale: [1, 1.18, 1.3] }}
            transition={{ delay: 1.7, duration: 0.8, ease: "easeOut" }}
          />
        )}
      </motion.div>
    </div>
  )
}

/* ── Checklist ── */

function Check({ done, reduce }: Readonly<{ done: boolean; reduce: boolean }>) {
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" className="shrink-0" aria-hidden="true">
      <circle cx="11" cy="11" r="10" fill="none" stroke={done ? GREEN : "rgba(255,255,255,0.22)"} strokeWidth="1.5" className="transition-colors duration-500" />
      <AnimatePresence>
        {done && (
          <motion.circle key="fill" cx="11" cy="11" r="10" fill={GREEN}
            initial={reduce ? false : { scale: 0.4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
            style={{ transformOrigin: "11px 11px" }} transition={{ duration: reduce ? 0 : 0.3, ease: EASE }} />
        )}
      </AnimatePresence>
      {done && (
        <motion.path d="M6.5 11.5l3 3 6-6.5" fill="none" stroke="#0a0a0a" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          initial={reduce ? false : { pathLength: 0 }} animate={{ pathLength: 1 }}
          transition={{ delay: reduce ? 0 : 0.15, duration: reduce ? 0 : 0.35, ease: EASE }} />
      )}
    </svg>
  )
}

function ProviderTiles({ ids, connected }: Readonly<{ ids: string[]; connected: Set<string> }>) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
      {ids.map((id) => {
        const c = connectorRegistry.get(id)
        if (!c) return null
        const isOn = connected.has(id)
        return (
          <Link
            key={id}
            href={c.path ?? "/connectors"}
            className={`group flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 ${isOn ? "border-[#2eb67d]/40 bg-[#2eb67d]/[0.07] text-white" : "border-white/[0.08] bg-white/[0.02] text-white/80 hover:border-white/20 hover:bg-white/[0.05] hover:text-white"}`}
          >
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white p-1">
              {c.iconPath ? <Image src={c.iconPath} alt="" width={16} height={16} className="h-4 w-4 object-contain" /> : null}
            </span>
            <span className="truncate">{c.name}</span>
          </Link>
        )
      })}
    </div>
  )
}

function StepRow({
  title, detail, done, open, onToggle, reduce, children,
}: Readonly<{
  title: string; detail: string; done: boolean; open: boolean
  onToggle?: () => void; reduce: boolean; children?: React.ReactNode
}>) {
  const header = (
    <div className="flex w-full items-start gap-3.5 text-left">
      <Check done={done} reduce={reduce} />
      <div className="min-w-0 flex-1">
        <p className={`text-[15px] font-medium ${done ? "text-white/60" : "text-white"}`}>{title}</p>
        <p className="mt-0.5 text-[13px] leading-relaxed text-white/45">{detail}</p>
      </div>
      {onToggle && (
        <motion.svg width="16" height="16" viewBox="0 0 24 24" className="mt-1 shrink-0 text-white/35" animate={{ rotate: open ? 180 : 0 }} transition={{ duration: reduce ? 0 : 0.25 }} aria-hidden="true">
          <path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </motion.svg>
      )}
    </div>
  )
  return (
    <li className="border-t border-white/[0.06] first:border-t-0">
      {onToggle ? (
        <button type="button" onClick={onToggle} aria-expanded={open} className="w-full px-5 py-4 rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-white/20">
          {header}
        </button>
      ) : (
        <div className="px-5 py-4">{header}</div>
      )}
      <AnimatePresence initial={false}>
        {open && children && (
          <motion.div
            initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={{ duration: reduce ? 0 : 0.35, ease: EASE }}
            className="overflow-hidden"
          >
            <div className="px-5 pb-5 pl-[3.25rem]">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  )
}

/* ── Page ── */

function SlackWelcome() {
  const params = useSearchParams()
  const reduce = useReducedMotion() ?? false
  const { data: session } = useSession()
  const [slack, setSlack] = useState<SlackStatus | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [demo, setDemo] = useState<"idle" | "sending" | "sent" | "error">("idle")
  const [copied, setCopied] = useState(false)
  const connected = useConnectorStatus()

  const joined = params.get("joined") === "1"
  const slackAuth = params.get("slack_auth")
  const failed = slackAuth === "failed" && slack?.connected !== true
  const canConnect = session?.user?.role !== ROLE_VIEWER

  useEffect(() => {
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
  }, [])

  const set = useMemo(() => connected ?? new Set<string>(), [connected])
  const alerts = useMemo(() => [...set].filter((id) => ALL_ALERT_IDS.has(id)), [set])
  const rootCause = useMemo(() => [...set].filter((id) => ALL_ROOT_CAUSE_IDS.has(id)), [set])
  const slackDone = slack?.connected === true || (slackAuth !== "failed" && set.has("slack"))
  const done: Record<StepId, boolean> = { slack: slackDone, alerts: alerts.length > 0, root_cause: rootCause.length > 0 }
  const doneCount = Object.values(done).filter(Boolean).length

  // The first unfinished step opens by itself; ?step= (from Slack's buttons) wins.
  const [openStep, setOpenStep] = useState<StepId | null>(null)
  const userToggled = useRef(false)
  useEffect(() => {
    if (userToggled.current || connected === null) return
    const asked = params.get("step")
    if (asked === "alerts" || asked === "root_cause") { setOpenStep(asked); return }
    setOpenStep(!done.alerts ? "alerts" : !done.root_cause ? "root_cause" : null)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, done.alerts, done.root_cause])
  const toggle = (id: StepId) => { userToggled.current = true; setOpenStep((cur) => (cur === id ? null : id)) }

  const retrySlack = async () => {
    setRetrying(true)
    try {
      const res = await fetch("/api/slack", { method: "POST" })
      const data = await res.json()
      if (data?.oauth_url) { globalThis.location.href = data.oauth_url; return }
    } catch { /* falls through to re-enable the button */ }
    setRetrying(false)
  }

  const runDemo = async () => {
    setDemo("sending")
    try {
      const res = await fetch("/api/slack/demo", { method: "POST" })
      setDemo(res.ok ? "sent" : "error")
    } catch {
      setDemo("error")
    }
  }

  const copyInvite = async () => {
    try {
      await navigator.clipboard.writeText(`${globalThis.location.origin}/sign-in`)
      setCopied(true)
      globalThis.setTimeout(() => setCopied(false), 2000)
    } catch { /* clipboard blocked: the link is visible to copy by hand */ }
  }

  const team = slack?.team_name
  const channel = slack?.incidents_channel_name
  const orgName = session?.user?.orgName

  let headline = team ? `Aurora is in ${team}` : "Aurora is in your Slack"
  let lede = channel
    ? `Root causes will post to #${channel}. Mention @Aurora in any channel to ask a follow-up.`
    : "Mention @Aurora in any channel to ask about an incident."
  if (joined) {
    headline = orgName ? `You're in ${orgName}` : "You're in"
    lede = "Your team already runs Aurora in Slack. Mention @Aurora in any channel to ask about an incident."
  } else if (failed) {
    headline = "Your account is ready"
    lede = "Aurora couldn't create its incidents channel in Slack. Check that the app is allowed to create channels, then try again."
  }

  // Text starts after the link has drawn; instantly when motion is reduced.
  const reveal = (delay: number) => ({
    initial: reduce ? false : { opacity: 0, y: 10 },
    animate: { opacity: 1, y: 0 },
    transition: reduce ? { duration: 0 } : { delay, duration: 0.5, ease: EASE },
  })

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto flex min-h-full w-full max-w-[600px] flex-col px-5 py-12 sm:py-20">
        <LinkMark reduce={reduce} failed={failed} />

        <motion.h1 {...reveal(1.25)} className="mt-9 text-[32px] sm:text-[40px] font-semibold leading-[1.1] tracking-[-0.02em] text-white text-balance">
          {headline}
        </motion.h1>
        <motion.p {...reveal(1.35)} className="mt-3 max-w-[52ch] text-[15px] sm:text-base leading-relaxed text-white/55">
          {lede}
        </motion.p>

        <motion.div {...reveal(1.45)} className="mt-7 flex flex-wrap items-center gap-3">
          {failed ? (
            <button type="button" onClick={retrySlack} disabled={retrying} className="inline-flex items-center gap-2.5 rounded-lg bg-white px-5 py-2.5 text-sm font-medium text-black hover:bg-white/90 disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 focus-visible:ring-offset-2 focus-visible:ring-offset-black transition-colors">
              <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
              {retrying ? "Opening Slack…" : "Try Slack again"}
            </button>
          ) : (
            <a href={slackDeepLink(slack)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2.5 rounded-lg bg-white px-5 py-2.5 text-sm font-medium text-black hover:bg-white/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 focus-visible:ring-offset-2 focus-visible:ring-offset-black transition-colors">
              <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
              Open Slack
            </a>
          )}
          {!failed && (
            <button
              type="button"
              onClick={runDemo}
              disabled={demo === "sending" || demo === "sent"}
              className="inline-flex items-center gap-2 rounded-lg border border-white/15 bg-white/[0.04] px-5 py-2.5 text-sm font-medium text-white hover:bg-white/[0.08] disabled:cursor-default focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 transition-colors"
            >
              <AnimatePresence mode="wait" initial={false}>
                <motion.span
                  key={demo}
                  initial={reduce ? false : { opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={reduce ? { opacity: 0 } : { opacity: 0, y: -4 }}
                  transition={{ duration: 0.18 }}
                  className="inline-flex items-center gap-2"
                >
                  {demo === "idle" && "Run a demo incident in Slack"}
                  {demo === "sending" && "Starting the demo…"}
                  {demo === "sent" && (<><span className="h-1.5 w-1.5 rounded-full" style={{ background: GREEN }} />Playing in your Aurora DM</>)}
                  {demo === "error" && "Couldn't start it. Try again"}
                </motion.span>
              </AnimatePresence>
            </button>
          )}
        </motion.div>

        <motion.section {...reveal(1.6)} aria-labelledby="setup-heading" className="mt-12 rounded-2xl border border-white/[0.08] bg-black/45 backdrop-blur-md">
          <div className="flex items-center justify-between gap-4 px-5 pt-5 pb-3">
            <h2 id="setup-heading" className="text-[15px] font-medium text-white">Get to your first real root cause</h2>
            <div className="flex shrink-0 items-center gap-2" aria-label={`${doneCount} of 3 steps done`}>
              <div className="flex gap-1">
                {[0, 1, 2].map((i) => (
                  <motion.span key={i} className="h-1 w-5 rounded-full" initial={false}
                    animate={{ backgroundColor: i < doneCount ? GREEN : "rgba(255,255,255,0.12)" }}
                    transition={{ duration: reduce ? 0 : 0.4, delay: reduce ? 0 : i * 0.05 }} />
                ))}
              </div>
              <span className="whitespace-nowrap text-xs tabular-nums text-white/45">{doneCount} of 3</span>
            </div>
          </div>
          <ol className="pb-1">
            <StepRow
              title="Add Aurora to Slack"
              detail={slackDone ? (channel ? `Connected. Posting to #${channel}.` : "Connected.") : "Not connected yet."}
              done={slackDone}
              open={false}
              reduce={reduce}
            />
            <StepRow
              title="Connect your alerts"
              detail={done.alerts ? `Connected: ${alerts.map((id) => connectorRegistry.get(id)?.name ?? id).join(", ")}.` : "Aurora starts investigating the moment an alert fires."}
              done={done.alerts}
              open={openStep === "alerts"}
              onToggle={() => toggle("alerts")}
              reduce={reduce}
            >
              {canConnect ? <ProviderTiles ids={ALERT_IDS} connected={set} /> : <p className="text-[13px] text-white/45">Ask an admin in {orgName ?? "your organization"} to connect your alerting tool.</p>}
            </StepRow>
            <StepRow
              title="Connect your cloud and code"
              detail={done.root_cause ? `Connected: ${rootCause.map((id) => connectorRegistry.get(id)?.name ?? id).join(", ")}.` : "So Aurora can read the logs, metrics and deploys behind each alert."}
              done={done.root_cause}
              open={openStep === "root_cause"}
              onToggle={() => toggle("root_cause")}
              reduce={reduce}
            >
              {canConnect ? <ProviderTiles ids={ROOT_CAUSE_IDS} connected={set} /> : <p className="text-[13px] text-white/45">Ask an admin in {orgName ?? "your organization"} to connect your cloud accounts.</p>}
            </StepRow>
          </ol>
        </motion.section>

        <motion.div {...reveal(1.75)} className="mt-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[13px] leading-relaxed text-white/45">
            Teammates don&apos;t need an invite. They sign in with Slack.{" "}
            <button type="button" onClick={copyInvite} className="text-white/75 underline decoration-white/25 underline-offset-4 hover:text-white hover:decoration-white/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 rounded">
              {copied ? "Link copied" : "Copy sign-in link"}
            </button>
          </p>
          <Link href="/" className="shrink-0 text-sm text-white/60 hover:text-white transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 rounded">
            Go to dashboard
          </Link>
        </motion.div>
      </div>
    </main>
  )
}

export default function SlackOnboardingPage() {
  return (
    <Suspense fallback={null}>
      <SlackWelcome />
    </Suspense>
  )
}

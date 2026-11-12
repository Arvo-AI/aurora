"use client"

import Image from "next/image"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "next/navigation"
import { useSession } from "next-auth/react"
import { AnimatePresence, motion, useReducedMotion } from "framer-motion"
import { ROLE_VIEWER } from "@/lib/roles"
import { useSlackSignInAvailable } from "@/app/sign-in/SlackSignIn"
import { useConnectedSet } from "@/hooks/use-connected-set"
import { useSetupJourney } from "@/hooks/use-setup-guide"
import { STACK, SETUP_RETURN_KEY, connectedIn, progress, type StackPartId } from "@/lib/onboarding/stack"
import StackMap from "./StackMap"
import SetupSteps from "./SetupSteps"
import GuideStrip from "./GuideStrip"

// One setup page for every way in: email signup, the GitHub App, Add to Slack.
// It is live: connect something in another tab and the map lights up here.

const GREEN = "#2eb67d"
const EASE = [0.22, 1, 0.36, 1] as const

type SlackStatus = {
  connected: boolean
  team_name?: string
  team_id?: string
  team_url?: string
  app_id?: string
  incidents_channel_name?: string
}

function slackDeepLink(s: SlackStatus | null): string {
  if (s?.app_id && s.team_id) return `https://slack.com/app_redirect?app=${encodeURIComponent(s.app_id)}&team=${encodeURIComponent(s.team_id)}`
  if (s?.team_url) return s.team_url
  return "https://app.slack.com/client"
}

function joinWords(words: string[]): string {
  if (words.length <= 1) return words.join("")
  return `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`
}

// Older links (Slack's welcome DM, App Home) name steps the old way.
function stepFromParam(v: string | null): StackPartId | null {
  if (v === "root_cause") return "cloud"
  return STACK.some((p) => p.id === v) ? (v as StackPartId) : null
}

function firstNameOf(name: string | null | undefined): string | null {
  const first = (name ?? "").trim().split(/\s+/)[0]
  // Accounts made from a GitHub login sometimes carry the handle as the name.
  if (!first || first.includes("@") || /\d/.test(first)) return null
  return first.charAt(0).toUpperCase() + first.slice(1)
}

export default function SetupHome({ via: viaProp }: Readonly<{ via?: string }>) {
  const params = useSearchParams()
  const reduce = useReducedMotion() ?? false
  const { data: session } = useSession()
  const { connected } = useConnectedSet(5000)
  const journey = useSetupJourney()
  const slackSignIn = useSlackSignInAvailable(true)

  const via = viaProp ?? params.get("via") ?? journey?.via ?? null
  const joined = params.get("joined") === "1"
  const slackAuth = params.get("slack_auth")
  const canConnect = session?.user?.role !== ROLE_VIEWER
  const orgName = session?.user?.orgName
  const guide = journey?.guide ?? null
  const first = journey?.first_name ?? firstNameOf(session?.user?.name)

  const set = useMemo(() => connected ?? new Set<string>(), [connected])
  const { done, total, next } = progress(set)
  const allDone = done === total

  // ── Slack details, when Slack is how they came in or is connected ──
  const [slack, setSlack] = useState<SlackStatus | null>(null)
  const slackRelevant = via === "slack" || set.has("slack")
  useEffect(() => {
    if (!slackRelevant) return
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
  }, [slackRelevant, set])
  const slackFailed = slackAuth === "failed" && slack?.connected !== true
  const slackOn = slack?.connected === true || set.has("slack")

  // ── Which step is open ──
  // The first unfinished one opens by itself and the page moves on to the next
  // as each connects, until someone opens a step by hand.
  const [openStep, setOpenStep] = useState<StackPartId | null>(null)
  const userChose = useRef(false)
  useEffect(() => {
    if (connected === null) return
    if (!userChose.current) {
      const asked = stepFromParam(params.get("step"))
      setOpenStep(asked && connectedIn(STACK.find((p) => p.id === asked)!, set).length === 0 ? asked : next?.id ?? null)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected])
  const toggle = useCallback((id: StackPartId) => {
    userChose.current = true
    setOpenStep((cur) => (cur === id ? null : id))
  }, [])
  const focusStep = useCallback((id: StackPartId) => {
    userChose.current = true
    setOpenStep(id)
    globalThis.requestAnimationFrame(() => {
      document.getElementById(`setup-${id}`)?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" })
    })
  }, [reduce])

  // Parts already connected on first load draw as one sequence.
  const [initiallyLit, setInitiallyLit] = useState<Set<StackPartId> | null>(null)
  useEffect(() => {
    if (connected && initiallyLit === null) {
      setInitiallyLit(new Set(STACK.filter((p) => connectedIn(p, connected).length > 0).map((p) => p.id)))
    }
  }, [connected, initiallyLit])

  // Arriving here means the way back worked; the app shouldn't offer it again.
  useEffect(() => {
    try { sessionStorage.removeItem(SETUP_RETURN_KEY) } catch { /* nothing to clear */ }
  }, [])

  // ── Words ──
  const doneShorts = STACK.filter((p) => connectedIn(p, set).length > 0).map((p) => p.short.toLowerCase())
  let headline = first ? `Welcome to Aurora, ${first}` : "Welcome to Aurora"
  let lede = "Connect where your alerts come from, where your services run and your code. Then Aurora can investigate your next incident on its own."
  if (allDone) {
    headline = "Aurora can see your whole stack"
    lede = "The next alert that fires gets a full investigation, posted where your team already talks. You can add more tools from Connectors any time."
  } else if (done > 0 && next) {
    headline = `Aurora can see your ${joinWords(doneShorts)}`
    const nextStep = `${next.title.charAt(0).toLowerCase()}${next.title.slice(1)}`
    lede = via === "github" && set.has("github") && done === 1
      ? `Aurora is already reviewing pull requests on the repos you picked. Next, ${nextStep}: ${next.why}`
      : `Next, ${nextStep}. ${next.why}`
  }
  if (via === "slack" && joined) {
    headline = orgName ? `You're in ${orgName}` : "You're in"
    lede = "Your team already runs Aurora. Mention @Aurora in any Slack channel to ask about an incident."
  } else if (via === "slack" && slackFailed) {
    headline = "Your account is ready"
    lede = "Aurora couldn't create its incidents channel in Slack. Check that the app is allowed to create channels, then try again."
  }

  // ── Slack actions ──
  const [retrying, setRetrying] = useState(false)
  const [demo, setDemo] = useState<"idle" | "sending" | "sent" | "error">("idle")
  const [copied, setCopied] = useState(false)
  const retrySlack = async () => {
    setRetrying(true)
    try {
      const res = await fetch("/api/slack", { method: "POST" })
      const data = await res.json()
      if (data?.oauth_url) { globalThis.location.href = data.oauth_url; return }
    } catch { /* re-enable the button */ }
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
    } catch { /* clipboard blocked */ }
  }

  // ── Leaving ──
  const [leaving, setLeaving] = useState(false)
  const goToAurora = async () => {
    setLeaving(true)
    try {
      await fetch("/api/onboarding/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_connectors: [...set].slice(0, 50) }),
      })
    } catch { /* not worth stopping someone for */ }
    globalThis.location.href = "/"
  }

  const ready = connected !== null
  const t0 = reduce ? 0 : 0.35
  const reveal = (delay: number) => ({
    initial: reduce ? false : { opacity: 0, y: 10 },
    animate: ready ? { opacity: 1, y: 0 } : { opacity: 0, y: 10 },
    transition: reduce ? { duration: 0 } : { delay: t0 + delay, duration: 0.5, ease: EASE },
  })

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto flex min-h-full w-full max-w-[640px] flex-col px-5 pb-16 pt-10 sm:pt-16">
        <div className="flex h-5 justify-end">
          {!allDone && (
            <button
              type="button"
              onClick={goToAurora}
              disabled={leaving}
              className="rounded text-sm text-white/45 transition-colors hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 disabled:opacity-60"
            >
              Skip for now
            </button>
          )}
        </div>

        <div className="mt-4">
          {ready ? (
            <StackMap connected={set} initiallyLit={initiallyLit} reduce={reduce} onSelect={focusStep} />
          ) : (
            <div className="mx-auto w-full max-w-[560px]" style={{ aspectRatio: "560 / 220" }} />
          )}
        </div>

        <AnimatePresence mode="wait" initial={false}>
          <motion.h1
            key={headline}
            {...reveal(0.1)}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: -6 }}
            className="mt-10 text-balance text-[32px] font-semibold leading-[1.1] tracking-[-0.02em] text-white sm:text-[40px]"
          >
            {headline}
          </motion.h1>
        </AnimatePresence>
        <motion.p {...reveal(0.18)} className="mt-3 max-w-[56ch] text-[15px] leading-relaxed text-white/55 sm:text-base">
          {lede}
        </motion.p>

        {(slackRelevant || slackFailed) && !joined && (
          <motion.div {...reveal(0.26)} className="mt-6 flex flex-wrap items-center gap-3">
            {slackFailed ? (
              <button type="button" onClick={retrySlack} disabled={retrying} className="inline-flex items-center gap-2.5 rounded-lg bg-white px-5 py-2.5 text-sm font-medium text-black transition-colors hover:bg-white/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 focus-visible:ring-offset-2 focus-visible:ring-offset-black disabled:opacity-60">
                <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
                {retrying ? "Opening Slack…" : "Try Slack again"}
              </button>
            ) : slackOn ? (
              <>
                <a href={slackDeepLink(slack)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2.5 rounded-lg border border-white/15 bg-white/[0.04] px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-white/[0.08] focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
                  <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
                  {slack?.team_name ? `Open ${slack.team_name}` : "Open Slack"}
                </a>
                <button
                  type="button"
                  onClick={runDemo}
                  disabled={demo === "sending" || demo === "sent"}
                  className="inline-flex items-center gap-2 rounded-lg border border-white/15 bg-white/[0.04] px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-white/[0.08] focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 disabled:cursor-default"
                >
                  <AnimatePresence mode="wait" initial={false}>
                    <motion.span key={demo} initial={reduce ? false : { opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={reduce ? { opacity: 0 } : { opacity: 0, y: -4 }} transition={{ duration: 0.18 }} className="inline-flex items-center gap-2">
                      {demo === "idle" && "Watch a demo incident in Slack"}
                      {demo === "sending" && "Starting the demo…"}
                      {demo === "sent" && (<><span className="h-1.5 w-1.5 rounded-full" style={{ background: GREEN }} />Playing in your Aurora DM</>)}
                      {demo === "error" && "Couldn't start it. Try again"}
                    </motion.span>
                  </AnimatePresence>
                </button>
              </>
            ) : null}
          </motion.div>
        )}

        {guide && (
          <motion.div {...reveal(0.34)} className="mt-10">
            <GuideStrip guide={guide} done={allDone} />
          </motion.div>
        )}

        <motion.div {...reveal(0.42)} className={guide ? "mt-4" : "mt-10"}>
          <SetupSteps
            connected={set}
            openStep={openStep}
            onToggle={toggle}
            reduce={reduce}
            canConnect={canConnect}
            orgName={orgName}
            guide={guide}
          />
        </motion.div>

        <motion.div {...reveal(0.5)} className="mt-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="max-w-[46ch] text-[13px] leading-relaxed text-white/45">
            {slackOn && slackSignIn ? (
              <>
                Teammates don&apos;t need an invite, they sign in with Slack.{" "}
                <button type="button" onClick={copyInvite} className="rounded text-white/75 underline decoration-white/25 underline-offset-4 hover:text-white hover:decoration-white/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
                  {copied ? "Link copied" : "Copy sign-in link"}
                </button>
              </>
            ) : (
              "Setup stays here while you work. Come back to it from the setup button in the corner."
            )}
          </p>
          <button
            type="button"
            onClick={goToAurora}
            disabled={leaving}
            className={`inline-flex shrink-0 items-center justify-center rounded-lg px-5 py-2.5 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 focus-visible:ring-offset-2 focus-visible:ring-offset-black disabled:opacity-60 ${
              allDone ? "bg-white text-black hover:bg-white/90" : "border border-white/15 text-white hover:bg-white/[0.06]"
            }`}
          >
            {leaving ? "Opening Aurora…" : "Go to Aurora"}
          </button>
        </motion.div>
      </div>
    </main>
  )
}

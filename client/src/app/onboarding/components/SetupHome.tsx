"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "next/navigation"
import { useSession } from "next-auth/react"
import { AnimatePresence, motion, useReducedMotion } from "framer-motion"
import { ROLE_VIEWER } from "@/lib/roles"
import { useSlackSignInAvailable } from "@/app/sign-in/SlackSignIn"
import { useConnectedSet } from "@/hooks/use-connected-set"
import { useSetupJourney } from "@/hooks/use-setup-guide"
import { STACK, SETUP_RETURN_KEY, connectedIn, progress, type StackPart, type StackPartId } from "@/lib/onboarding/stack"
import StackMap from "@/app/onboarding/components/StackMap"
import SetupSteps from "@/app/onboarding/components/SetupSteps"
import GuideStrip from "@/app/onboarding/components/GuideStrip"
import FirstQuestions from "@/app/onboarding/components/FirstQuestions"
import SlackActions, { useSlackStatus } from "@/app/onboarding/components/SlackActions"
import { describeSetup } from "@/app/onboarding/components/setup-copy"

// One setup page for every way in: email signup, the GitHub App, Add to Slack.
// It is live: connect something in another tab and the map lights up here.

const EASE = [0.22, 1, 0.36, 1] as const

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

// The first unfinished step opens by itself, and the page moves on to the
// next as each connects, until someone opens a step by hand.
function useOpenStep(connected: Set<string> | null, next: StackPart | null, asked: StackPartId | null, reduce: boolean) {
  const [openStep, setOpenStep] = useState<StackPartId | null>(null)
  const userChose = useRef(false)
  useEffect(() => {
    if (connected === null || userChose.current) return
    const askedPart = STACK.find((p) => p.id === asked)
    const askedOpen = askedPart && connectedIn(askedPart, connected).length === 0
    setOpenStep(askedOpen ? askedPart.id : next?.id ?? null)
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
  return { openStep, toggle, focusStep }
}

// Parts already connected on first load draw as one sequence.
function useInitiallyLit(connected: Set<string> | null) {
  const [lit, setLit] = useState<Set<StackPartId> | null>(null)
  useEffect(() => {
    if (connected && lit === null) {
      setLit(new Set(STACK.filter((p) => connectedIn(p, connected).length > 0).map((p) => p.id)))
    }
  }, [connected, lit])
  return lit
}

function useLeave(connected: ReadonlySet<string> | null) {
  const [leaving, setLeaving] = useState(false)
  const leave = async () => {
    setLeaving(true)
    // Without a status answer there's nothing true to save; the saved
    // selections stay as they were.
    if (connected === null) {
      globalThis.location.href = "/"
      return
    }
    try {
      await fetch("/api/onboarding/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_connectors: [...connected].slice(0, 50) }),
      })
    } catch { /* not worth stopping someone for */ }
    globalThis.location.href = "/"
  }
  return { leaving, leave }
}

function TeammatesLine({ slackOn }: Readonly<{ slackOn: boolean }>) {
  const slackSignIn = useSlackSignInAvailable(true)
  const [copied, setCopied] = useState(false)
  if (!(slackOn && slackSignIn)) {
    return <>Setup stays here while you work. Come back to it from the setup button in the corner.</>
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(`${globalThis.location.origin}/sign-in`)
      setCopied(true)
      globalThis.setTimeout(() => setCopied(false), 2000)
    } catch { /* clipboard blocked */ }
  }
  return (
    <>
      Teammates don&apos;t need an invite, they sign in with Slack.{" "}
      <button type="button" onClick={copy} className="rounded text-white/75 underline decoration-white/25 underline-offset-4 hover:text-white hover:decoration-white/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
        {copied ? "Link copied" : "Copy sign-in link"}
      </button>
    </>
  )
}

export default function SetupHome({ via: viaProp }: Readonly<{ via?: string }>) {
  const params = useSearchParams()
  const reduce = useReducedMotion() ?? false
  const { data: session } = useSession()
  const { connected, failed: statusFailed } = useConnectedSet(5000)
  const journey = useSetupJourney()

  const via = viaProp ?? params.get("via") ?? journey?.via ?? null
  const joined = params.get("joined") === "1"
  const canConnect = session?.user?.role !== ROLE_VIEWER
  const orgName = session?.user?.orgName
  const guide = journey?.guide ?? null

  const set = useMemo(() => connected ?? new Set<string>(), [connected])
  const { next } = progress(set)
  const allDone = next === null

  const slack = useSlackStatus(via === "slack" || set.has("slack"), set)
  const slackFailed = params.get("slack_auth") === "failed" && slack?.connected !== true
  const slackOn = slack?.connected === true || set.has("slack")

  const { openStep, toggle, focusStep } = useOpenStep(connected, next, stepFromParam(params.get("step")), reduce)
  const initiallyLit = useInitiallyLit(connected)
  const { leaving, leave } = useLeave(connected)

  // Arriving here means the way back worked; the app shouldn't offer it again.
  useEffect(() => {
    try { sessionStorage.removeItem(SETUP_RETURN_KEY) } catch { /* nothing to clear */ }
  }, [])

  const { headline, lede } = describeSetup({
    first: journey?.first_name ?? firstNameOf(session?.user?.name),
    connected: set, next, via, joined, slackFailed, orgName,
  })

  // The page arrives in one piece: wait for the connection status (or its
  // failure) and the guide, so nothing pops in or reflows afterwards. Then
  // everything rises together, top to bottom, a beat apart; the aurora lines
  // draw once it has settled.
  const ready = (connected !== null || statusFailed) && journey !== null
  const reveal = (step: number) => ({
    initial: reduce ? false : { opacity: 0, y: 8 },
    animate: ready ? { opacity: 1, y: 0 } : { opacity: 0, y: 8 },
    transition: reduce ? { duration: 0 } : { delay: step * 0.04, duration: 0.55, ease: EASE },
  })

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto flex min-h-full w-full max-w-[640px] flex-col px-5 pb-16 pt-10 sm:pt-16">
        <div className="flex h-5 justify-end">
          {!allDone && (
            <motion.button
              {...reveal(0)}
              type="button"
              onClick={leave}
              disabled={leaving}
              className="rounded text-sm text-white/45 transition-colors hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 disabled:opacity-60"
            >
              Skip for now
            </motion.button>
          )}
        </div>

        <motion.div {...reveal(0)} className="mt-4">
          {ready ? (
            <StackMap connected={set} initiallyLit={initiallyLit} reduce={reduce} onSelect={focusStep} />
          ) : (
            <div className="mx-auto w-full max-w-[560px]" style={{ aspectRatio: "560 / 220" }} />
          )}
        </motion.div>

        <AnimatePresence mode="wait" initial={false}>
          <motion.h1
            key={headline}
            {...reveal(1)}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: -6 }}
            className="mt-10 text-balance text-[32px] font-semibold leading-[1.1] tracking-[-0.02em] text-white sm:text-[40px]"
          >
            {headline}
          </motion.h1>
        </AnimatePresence>
        <motion.p {...reveal(2)} className="mt-3 max-w-[56ch] text-[15px] leading-relaxed text-white/55 sm:text-base">
          {lede}
        </motion.p>

        {statusFailed && connected === null && (
          <output className="mt-4 block text-[13px] text-amber-200/80">
            Aurora couldn&apos;t check your connections just now. It will try again in a few seconds; you can still connect tools below.
          </output>
        )}

        {!joined && (
          <motion.div {...reveal(3)} className="mt-6 flex flex-wrap items-center gap-3 empty:hidden">
            <SlackActions slack={slack} failed={via === "slack" && slackFailed} on={slackOn} reduce={reduce} />
          </motion.div>
        )}

        {guide && (
          <motion.div {...reveal(3)} className="mt-10">
            <GuideStrip guide={guide} team={journey?.team ?? "Aurora"} done={allDone} />
          </motion.div>
        )}

        <motion.div {...reveal(4)} className={guide ? "mt-4" : "mt-10"}>
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

        <motion.div {...reveal(5)} className="mt-4 empty:hidden">
          <FirstQuestions connected={set} />
        </motion.div>

        <motion.div {...reveal(6)} className="mt-6 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="max-w-[46ch] text-[13px] leading-relaxed text-white/45">
            <TeammatesLine slackOn={slackOn} />
          </p>
          <button
            type="button"
            onClick={leave}
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

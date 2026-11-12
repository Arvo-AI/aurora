"use client"

import Image from "next/image"
import Link from "next/link"
import { useEffect, useMemo, useRef, useState, type RefObject } from "react"
import { usePathname } from "next/navigation"
import { useSession } from "next-auth/react"
import { AnimatePresence, motion, useReducedMotion } from "framer-motion"
import { X } from "lucide-react"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import { ROLE_VIEWER } from "@/lib/roles"
import { useConnectedSet } from "@/hooks/use-connected-set"
import { useSetupJourney, type SetupGuide } from "@/hooks/use-setup-guide"
import { STACK, SETUP_RETURN_KEY, connectedIn, partOf, progress, type StackPart } from "@/lib/onboarding/stack"
import { Check } from "@/app/onboarding/components/SetupSteps"
import GuideAvatar from "@/components/onboarding/GuideAvatar"

// Lives in the corner of the app until the stack is connected: how far setup
// got, who to ask, and a nudge back to setup the moment a tool connects.

const GREEN = "#2eb67d"
const EASE = [0.22, 1, 0.36, 1] as const
const MIN_KEY = "aurora_setup_companion_min"
const NOTICE_MS = 10_000
const CARD = "w-[min(340px,calc(100vw-2.5rem))] rounded-2xl border border-white/10 bg-[#0d0f10]/95 shadow-[0_24px_60px_-20px_rgba(0,0,0,0.9)] backdrop-blur-xl"

function readMin(): boolean {
  try { return localStorage.getItem(MIN_KEY) === "1" } catch { return false }
}
function writeMin(v: boolean) {
  try { if (v) localStorage.setItem(MIN_KEY, "1"); else localStorage.removeItem(MIN_KEY) } catch { /* per-browser nicety only */ }
}
function cameFromSetup(): boolean {
  try { return Boolean(sessionStorage.getItem(SETUP_RETURN_KEY)) } catch { return false }
}
function nextLine(next: StackPart): string {
  return `Next: ${next.title.charAt(0).toLowerCase()}${next.title.slice(1)}.`
}

function Ring({ done, total, size = 18 }: Readonly<{ done: number; total: number; size?: number }>) {
  // One arc per part of the stack, lit as each connects.
  const r = 7.5
  const c = 2 * Math.PI * r
  const gap = 2.2
  const seg = c / total - gap
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" className="-rotate-90" aria-hidden="true">
      {Array.from({ length: total }, (_, i) => (
        <circle
          key={i} cx="10" cy="10" r={r} fill="none" strokeWidth="2.5" strokeLinecap="round"
          stroke={i < done ? GREEN : "rgba(255,255,255,0.16)"}
          strokeDasharray={`${seg} ${c - seg}`}
          strokeDashoffset={-i * (seg + gap)}
          className="transition-[stroke] duration-500"
        />
      ))}
    </svg>
  )
}

// New connections since the last poll, as a notice that clears itself.
function useConnectNotice(connected: Set<string> | null) {
  const [notice, setNotice] = useState<string[] | null>(null)
  const prev = useRef<Set<string> | null>(null)
  useEffect(() => {
    if (!connected) return
    const before = prev.current
    prev.current = connected
    if (!before) return
    const added = [...connected].filter((id) => !before.has(id) && partOf(id))
    if (added.length) setNotice(added)
  }, [connected])
  useEffect(() => {
    if (!notice) return
    const t = globalThis.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => globalThis.clearTimeout(t)
  }, [notice])
  return { notice, clear: () => setNotice(null) }
}

// Escape and clicks outside close the panel; opening moves focus into it and
// closing with Escape puts focus back on the button.
function usePanelFocus(open: boolean, close: () => void, root: RefObject<HTMLDivElement | null>,
                       panel: RefObject<HTMLDivElement | null>, button: RefObject<HTMLButtonElement | null>) {
  useEffect(() => {
    if (!open) return
    globalThis.requestAnimationFrame(() => panel.current?.querySelector<HTMLElement>("a, button")?.focus())
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return
      close()
      button.current?.focus()
    }
    const onClick = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) close()
    }
    globalThis.addEventListener("keydown", onKey)
    globalThis.addEventListener("mousedown", onClick)
    return () => { globalThis.removeEventListener("keydown", onKey); globalThis.removeEventListener("mousedown", onClick) }
  }, [open, close, root, panel, button])
}

function ConnectNotice({ ids, next, reduce, onDismiss }: Readonly<{
  ids: string[]; next: StackPart | null; reduce: boolean; onDismiss: () => void
}>) {
  const name = ids.map((id) => connectorRegistry.get(id)?.name ?? id).join(" and ")
  const icon = connectorRegistry.get(ids[0])?.iconPath
  const back = cameFromSetup() ? "bg-white text-black hover:bg-white/90" : "border border-white/15 text-white hover:bg-white/[0.06]"
  return (
    <motion.div
      key="notice"
      role="status"
      initial={reduce ? false : { opacity: 0, y: 12, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={reduce ? { opacity: 0 } : { opacity: 0, y: 8 }}
      transition={{ duration: reduce ? 0 : 0.35, ease: EASE }}
      className={`${CARD} overflow-hidden`}
    >
      <div className="h-[2px] w-full" style={{ background: "linear-gradient(90deg, #26e680, #1a99cc 55%, #9926b3)" }} />
      <div className="flex items-start gap-3 p-4">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white p-1.5">
          {icon ? <Image src={icon} alt="" width={22} height={22} className="h-full w-full object-contain" /> : null}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-white">{name} connected</p>
          <p className="mt-0.5 text-[13px] leading-relaxed text-white/50">
            {next ? nextLine(next) : "That's your whole stack. Aurora is ready for the next alert."}
          </p>
          {next && (
            <Link
              href={`/onboarding?step=${next.id}`}
              onClick={onDismiss}
              className={`mt-3 inline-flex items-center rounded-lg px-3.5 py-1.5 text-[13px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 ${back}`}
            >
              Back to setup
            </Link>
          )}
        </div>
        <button type="button" onClick={onDismiss} aria-label="Dismiss" className="-mr-1 -mt-1 rounded p-1 text-white/35 hover:text-white/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
          <X className="h-4 w-4" />
        </button>
      </div>
    </motion.div>
  )
}

function panelTitle(allDone: boolean, guide: SetupGuide | null): string {
  if (allDone) return "Aurora can see your whole stack"
  if (guide) return `${guide.first_name} is helping you set up`
  return "Finish setting up Aurora"
}

function GuideLinks({ guide }: Readonly<{ guide: SetupGuide | null }>) {
  if (!guide) return null
  return (
    <>
      {guide.booking_url && (
        <a href={guide.booking_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center rounded-lg border border-white/15 px-3.5 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-white/[0.06] focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
          Book a call
        </a>
      )}
      {guide.email && (
        <a href={`mailto:${guide.email}?subject=${encodeURIComponent("Aurora setup")}`} className="ml-auto rounded text-[13px] text-white/50 underline decoration-white/20 underline-offset-4 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
          Email
        </a>
      )}
    </>
  )
}

function CompanionPanel({ panelRef, connected, guide, allDone, canConnect, reduce, onClose, onShrink }: Readonly<{
  panelRef: RefObject<HTMLDivElement>; connected: ReadonlySet<string>; guide: SetupGuide | null
  allDone: boolean; canConnect: boolean; reduce: boolean; onClose: () => void; onShrink: () => void
}>) {
  const { done, total } = progress(connected)
  return (
    <motion.div
      key="panel"
      ref={panelRef}
      id="setup-companion-panel"
      role="dialog"
      aria-label="Setup"
      initial={reduce ? false : { opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={reduce ? { opacity: 0 } : { opacity: 0, y: 8, scale: 0.98 }}
      transition={{ duration: reduce ? 0 : 0.25, ease: EASE }}
      style={{ transformOrigin: "bottom right" }}
      className={CARD}
    >
      <div className="flex items-center gap-3 px-4 pb-3 pt-4">
        {guide ? <GuideAvatar guide={guide} size={36} /> : null}
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-white">{panelTitle(allDone, guide)}</p>
          <p className="mt-0.5 text-xs text-white/45">{allDone ? "Questions still welcome." : `${done} of ${total} connected`}</p>
        </div>
      </div>

      <ul className="border-t border-white/[0.06] py-1.5">
        {STACK.map((p) => {
          const ids = connectedIn(p, connected)
          const on = ids.length > 0
          return (
            <li key={p.id}>
              <Link
                href={`/onboarding?step=${p.id}`}
                onClick={onClose}
                className="flex items-center gap-3 px-4 py-2 text-[13px] transition-colors hover:bg-white/[0.04] focus:outline-none focus-visible:bg-white/[0.06]"
              >
                <Check done={on} reduce={reduce} size={18} />
                <span className={on ? "text-white/55" : "text-white/85"}>{p.short}</span>
                <span className="ml-auto truncate text-white/35">
                  {on ? ids.map((id) => connectorRegistry.get(id)?.name ?? id).join(", ") : "Not connected"}
                </span>
              </Link>
            </li>
          )
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-2 border-t border-white/[0.06] px-4 py-3.5">
        {!allDone && canConnect && (
          <Link href="/onboarding" onClick={onClose} className="inline-flex items-center rounded-lg bg-white px-3.5 py-1.5 text-[13px] font-medium text-black transition-colors hover:bg-white/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40">
            Continue setup
          </Link>
        )}
        <GuideLinks guide={guide} />
      </div>
      {!allDone && (
        <button
          type="button"
          onClick={onShrink}
          className="w-full rounded-b-2xl border-t border-white/[0.06] py-2 text-xs text-white/35 transition-colors hover:text-white/70 focus:outline-none focus-visible:bg-white/[0.04]"
        >
          Make this smaller
        </button>
      )}
    </motion.div>
  )
}

export default function SetupCompanion() {
  const pathname = usePathname()
  const reduce = useReducedMotion() ?? false
  const { data: session, status } = useSession()
  const signedIn = status === "authenticated"
  // Once the whole stack is connected there is nothing left to watch for.
  const [settled, setSettled] = useState(false)
  const { connected } = useConnectedSet(signedIn && !settled ? 30_000 : null)
  const journey = useSetupJourney()
  const [open, setOpen] = useState(false)
  const [min, setMin] = useState(false)
  const { notice, clear } = useConnectNotice(connected)
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const close = useMemo(() => () => setOpen(false), [])
  usePanelFocus(open, close, rootRef, panelRef, buttonRef)

  useEffect(() => { setMin(readMin()) }, [])

  const set = useMemo(() => connected ?? new Set<string>(), [connected])
  const { done, total, next } = progress(set)
  const allDone = next === null
  const guide = journey?.guide ?? null
  const isViewer = session?.user?.role === ROLE_VIEWER
  useEffect(() => { setSettled(allDone) }, [allDone])

  if (!signedIn || connected === null || journey === null) return null
  if (pathname?.startsWith("/onboarding")) return null
  // Without a guide there is nothing to offer once setup is over, except the
  // notice for the connection that finished it.
  if (!guide && (isViewer || allDone) && !notice) return null

  const label = allDone ? "Need a hand?" : `Setup ${done} of ${total}`
  const showPill = Boolean(guide) || !allDone

  return (
    <div ref={rootRef} className="fixed bottom-5 right-5 z-50 flex flex-col items-end gap-3 print:hidden">
      <AnimatePresence>
        {notice && !open && <ConnectNotice ids={notice} next={next} reduce={reduce} onDismiss={clear} />}
      </AnimatePresence>

      <AnimatePresence>
        {open && (
          <CompanionPanel
            panelRef={panelRef}
            connected={set}
            guide={guide}
            allDone={allDone}
            canConnect={!isViewer}
            reduce={reduce}
            onClose={close}
            onShrink={() => { setOpen(false); setMin(true); writeMin(true) }}
          />
        )}
      </AnimatePresence>

      {showPill && (
        <motion.button
          ref={buttonRef}
          type="button"
          layout
          onClick={() => {
            if (min) { setMin(false); writeMin(false) }
            setOpen((o) => !o)
          }}
          aria-expanded={open}
          aria-controls="setup-companion-panel"
          aria-label={min ? label : undefined}
          transition={{ duration: reduce ? 0 : 0.25, ease: EASE }}
          className="flex items-center gap-2.5 rounded-full border border-white/10 bg-[#0d0f10]/90 py-1.5 pl-2 pr-1.5 text-[13px] text-white/80 shadow-[0_12px_32px_-12px_rgba(0,0,0,0.9)] backdrop-blur-xl transition-colors hover:border-white/20 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
        >
          {!allDone && <span className={min ? "px-0.5" : "pl-0.5"}><Ring done={done} total={total} /></span>}
          {!min && <span className={`whitespace-nowrap ${allDone ? "pl-1.5" : ""}`}>{label}</span>}
          {guide ? <GuideAvatar guide={guide} size={26} /> : <span className="w-1" />}
        </motion.button>
      )}
    </div>
  )
}

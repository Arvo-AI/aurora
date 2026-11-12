"use client"

import Image from "next/image"
import Link from "next/link"
import { useEffect, useMemo, useRef, useState } from "react"
import { usePathname } from "next/navigation"
import { useSession } from "next-auth/react"
import { AnimatePresence, motion, useReducedMotion } from "framer-motion"
import { X } from "lucide-react"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import { ROLE_VIEWER } from "@/lib/roles"
import { useConnectedSet } from "@/hooks/use-connected-set"
import { useSetupJourney } from "@/hooks/use-setup-guide"
import { STACK, SETUP_RETURN_KEY, connectedIn, partOf, progress } from "@/lib/onboarding/stack"
import { Check } from "@/app/onboarding/components/SetupSteps"
import GuideAvatar from "./GuideAvatar"

// Lives in the corner of the app until the stack is connected: how far setup
// got, who to ask, and a nudge back to setup the moment a tool connects.

const GREEN = "#2eb67d"
const EASE = [0.22, 1, 0.36, 1] as const
const MIN_KEY = "aurora_setup_companion_min"
const NOTICE_MS = 10_000

function readMin(): boolean {
  try { return localStorage.getItem(MIN_KEY) === "1" } catch { return false }
}
function writeMin(v: boolean) {
  try { if (v) localStorage.setItem(MIN_KEY, "1"); else localStorage.removeItem(MIN_KEY) } catch { /* per-browser nicety only */ }
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
  const [notice, setNotice] = useState<{ ids: string[] } | null>(null)
  const prev = useRef<Set<string> | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => { setMin(readMin()) }, [])

  // A connection that lands while someone is in the app gets a moment of its own.
  useEffect(() => {
    if (!connected) return
    const before = prev.current
    prev.current = connected
    if (!before) return
    const added = [...connected].filter((id) => !before.has(id) && partOf(id))
    if (added.length) setNotice({ ids: added })
  }, [connected])

  useEffect(() => {
    if (!notice) return
    const t = globalThis.setTimeout(() => setNotice(null), NOTICE_MS)
    return () => globalThis.clearTimeout(t)
  }, [notice])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false) }
    const onClick = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false)
    }
    globalThis.addEventListener("keydown", onKey)
    globalThis.addEventListener("mousedown", onClick)
    return () => { globalThis.removeEventListener("keydown", onKey); globalThis.removeEventListener("mousedown", onClick) }
  }, [open])

  const set = useMemo(() => connected ?? new Set<string>(), [connected])
  const { done, total, next } = progress(set)
  const allDone = done === total
  const guide = journey?.guide ?? null
  const isViewer = session?.user?.role === ROLE_VIEWER
  useEffect(() => { setSettled(allDone) }, [allDone])

  if (!signedIn || connected === null || journey === null) return null
  if (pathname?.startsWith("/onboarding")) return null
  if (allDone && !guide) return null
  if (isViewer && !guide) return null

  const cameFromSetup = (() => { try { return Boolean(sessionStorage.getItem(SETUP_RETURN_KEY)) } catch { return false } })()
  const noticeName = notice ? notice.ids.map((id) => connectorRegistry.get(id)?.name ?? id).join(" and ") : ""
  const noticeIcon = notice ? connectorRegistry.get(notice.ids[0])?.iconPath : undefined
  const label = allDone ? "Need a hand?" : `Setup ${done} of ${total}`

  return (
    <div ref={panelRef} className="fixed bottom-5 right-5 z-50 flex flex-col items-end gap-3 print:hidden">
      <AnimatePresence>
        {notice && !open && (
          <motion.div
            key="notice"
            role="status"
            initial={reduce ? false : { opacity: 0, y: 12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: 8 }}
            transition={{ duration: reduce ? 0 : 0.35, ease: EASE }}
            className="w-[min(340px,calc(100vw-2.5rem))] overflow-hidden rounded-2xl border border-white/10 bg-[#0d0f10]/95 shadow-[0_24px_60px_-20px_rgba(0,0,0,0.9)] backdrop-blur-xl"
          >
            <div className="h-[2px] w-full" style={{ background: "linear-gradient(90deg, #26e680, #1a99cc 55%, #9926b3)" }} />
            <div className="flex items-start gap-3 p-4">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white p-1.5">
                {noticeIcon ? <Image src={noticeIcon} alt="" width={22} height={22} className="h-full w-full object-contain" /> : null}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-white">{noticeName} connected</p>
                <p className="mt-0.5 text-[13px] leading-relaxed text-white/50">
                  {allDone
                    ? "That's your whole stack. Aurora is ready for the next alert."
                    : next ? `Next: ${next.title.charAt(0).toLowerCase()}${next.title.slice(1)}.` : ""}
                </p>
                {!allDone && (
                  <Link
                    href={`/onboarding${next ? `?step=${next.id}` : ""}`}
                    onClick={() => setNotice(null)}
                    className={`mt-3 inline-flex items-center rounded-lg px-3.5 py-1.5 text-[13px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 ${
                      cameFromSetup ? "bg-white text-black hover:bg-white/90" : "border border-white/15 text-white hover:bg-white/[0.06]"
                    }`}
                  >
                    Back to setup
                  </Link>
                )}
              </div>
              <button type="button" onClick={() => setNotice(null)} aria-label="Dismiss" className="-mr-1 -mt-1 rounded p-1 text-white/35 hover:text-white/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
                <X className="h-4 w-4" />
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {open && (
          <motion.div
            key="panel"
            id="setup-companion-panel"
            role="dialog"
            aria-label="Setup"
            initial={reduce ? false : { opacity: 0, y: 10, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: reduce ? 0 : 0.25, ease: EASE }}
            style={{ transformOrigin: "bottom right" }}
            className="w-[min(340px,calc(100vw-2.5rem))] rounded-2xl border border-white/10 bg-[#0d0f10]/95 shadow-[0_24px_60px_-20px_rgba(0,0,0,0.9)] backdrop-blur-xl"
          >
            <div className="flex items-center gap-3 px-4 pb-3 pt-4">
              {guide ? <GuideAvatar guide={guide} size={36} /> : null}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-white">
                  {allDone ? "Aurora can see your whole stack" : guide ? `${guide.first_name} is helping you set up` : "Finish setting up Aurora"}
                </p>
                <p className="mt-0.5 text-xs text-white/45">
                  {allDone ? "Questions still welcome." : `${done} of ${total} connected`}
                </p>
              </div>
            </div>

            <ul className="border-t border-white/[0.06] py-1.5">
              {STACK.map((p) => {
                const ids = connectedIn(p, set)
                const on = ids.length > 0
                return (
                  <li key={p.id}>
                    <Link
                      href={`/onboarding?step=${p.id}`}
                      onClick={() => setOpen(false)}
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
              {!allDone && !isViewer && (
                <Link href="/onboarding" onClick={() => setOpen(false)} className="inline-flex items-center rounded-lg bg-white px-3.5 py-1.5 text-[13px] font-medium text-black transition-colors hover:bg-white/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40">
                  Continue setup
                </Link>
              )}
              {guide?.booking_url && (
                <a href={guide.booking_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center rounded-lg border border-white/15 px-3.5 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-white/[0.06] focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
                  Book a call
                </a>
              )}
              {guide?.email && (
                <a href={`mailto:${guide.email}?subject=${encodeURIComponent("Aurora setup")}`} className="ml-auto rounded text-[13px] text-white/50 underline decoration-white/20 underline-offset-4 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30">
                  Email
                </a>
              )}
            </div>
            {!allDone && (
              <button
                type="button"
                onClick={() => { setOpen(false); setMin(true); writeMin(true) }}
                className="w-full rounded-b-2xl border-t border-white/[0.06] py-2 text-xs text-white/35 transition-colors hover:text-white/70 focus:outline-none focus-visible:bg-white/[0.04]"
              >
                Make this smaller
              </button>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      <motion.button
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
    </div>
  )
}

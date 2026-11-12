"use client"

import Image from "next/image"
import { useEffect, useRef } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { Bell, Cloud, GitBranch, MessageSquare, type LucideIcon } from "lucide-react"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import { STACK, connectedIn, type StackPartId } from "@/lib/onboarding/stack"

// Aurora in the middle, the four parts of someone's stack around it. A part
// that is connected gets a line of aurora light running into Aurora; that is
// the one moving thing on the page, and it only moves when something connects.

const W = 560
const H = 220
const EASE = [0.22, 1, 0.36, 1] as const

const NODES: Record<StackPartId, { x: number; y: number; path: string; from: string; to: string }> = {
  alerts: { x: 64, y: 52, path: "M 94 52 C 178 52 188 110 240 110", from: "#26e680", to: "#1a99cc" },
  cloud: { x: 64, y: 168, path: "M 94 168 C 178 168 188 110 240 110", from: "#26e680", to: "#1a99cc" },
  code: { x: 496, y: 52, path: "M 466 52 C 382 52 372 110 320 110", from: "#9926b3", to: "#1a99cc" },
  chat: { x: 496, y: 168, path: "M 466 168 C 382 168 372 110 320 110", from: "#9926b3", to: "#1a99cc" },
}

const ICONS: Record<StackPartId, LucideIcon> = {
  alerts: Bell,
  cloud: Cloud,
  code: GitBranch,
  chat: MessageSquare,
}

const pct = (v: number, of: number) => `${(v / of) * 100}%`

// Lit on first load: draw in sequence. Lit later: draw right away.
function drawDelay(reduce: boolean, sequenceIndex: number): number {
  if (reduce) return 0
  if (sequenceIndex >= 0) return 0.25 + sequenceIndex * 0.18
  return 0.05
}

function Spark({ path, delay }: Readonly<{ path: string; delay: number }>) {
  // SMIL begin times count from page load, so a spark added later is started
  // by hand; begin="indefinite" keeps it still until then.
  const move = useRef<SVGAnimateMotionElement>(null)
  const fade = useRef<SVGAnimateElement>(null)
  useEffect(() => {
    const t = globalThis.setTimeout(() => {
      try {
        move.current?.beginElement()
        fade.current?.beginElement()
      } catch { /* SMIL unavailable: the line still draws */ }
    }, delay * 1000)
    return () => globalThis.clearTimeout(t)
  }, [delay])
  return (
    <circle r="3" fill="#fff" opacity="0" style={{ filter: "drop-shadow(0 0 6px rgba(255,255,255,0.9))" }}>
      <animateMotion ref={move} dur="0.75s" begin="indefinite" fill="freeze" path={path} keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.4 0 0.2 1" />
      <animate ref={fade} attributeName="opacity" values="0;1;1;0" keyTimes="0;0.1;0.85;1" dur="0.75s" begin="indefinite" fill="freeze" />
    </circle>
  )
}

interface StackMapProps {
  connected: ReadonlySet<string>
  // Parts lit when the page first rendered draw in one sequence; parts that
  // light up later draw on their own, with a spark, as the answer to a connect.
  initiallyLit: ReadonlySet<StackPartId> | null
  reduce: boolean
  onSelect: (id: StackPartId) => void
}

export default function StackMap({ connected, initiallyLit, reduce, onSelect }: Readonly<StackMapProps>) {
  const allDone = STACK.every((p) => connectedIn(p, connected).length > 0)
  const initialOrder = STACK.filter((p) => initiallyLit?.has(p.id)).map((p) => p.id)

  return (
    <div className="relative mx-auto w-full max-w-[560px] select-none" style={{ aspectRatio: `${W} / ${H}` }}>
      <svg viewBox={`0 0 ${W} ${H}`} className="absolute inset-0 h-full w-full overflow-visible" aria-hidden="true">
        <defs>
          {STACK.map((p) => {
            const n = NODES[p.id]
            return (
              <linearGradient key={p.id} id={`stack-grad-${p.id}`} gradientUnits="userSpaceOnUse" x1={n.x} y1={n.y} x2={W / 2} y2={H / 2}>
                <stop offset="0%" stopColor={n.from} />
                <stop offset="100%" stopColor={n.to} />
              </linearGradient>
            )
          })}
        </defs>
        {STACK.map((p) => {
          const n = NODES[p.id]
          const lit = connectedIn(p, connected).length > 0
          const seq = initialOrder.indexOf(p.id)
          const delay = drawDelay(reduce, seq)
          return (
            <g key={p.id}>
              <path d={n.path} fill="none" stroke="rgba(255,255,255,0.12)" strokeWidth="1.5" strokeDasharray="2 5" strokeLinecap="round" />
              <AnimatePresence>
                {lit && (
                  <motion.g key="lit" initial={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.3 }}>
                    <motion.path
                      d={n.path} fill="none" stroke={`url(#stack-grad-${p.id})`} strokeWidth="6" strokeLinecap="round" opacity="0.28"
                      style={{ filter: "blur(4px)" }}
                      initial={reduce ? false : { pathLength: 0 }} animate={{ pathLength: 1 }}
                      transition={{ delay, duration: reduce ? 0 : 0.7, ease: EASE }}
                    />
                    <motion.path
                      d={n.path} fill="none" stroke={`url(#stack-grad-${p.id})`} strokeWidth="2" strokeLinecap="round"
                      initial={reduce ? false : { pathLength: 0 }} animate={{ pathLength: 1 }}
                      transition={{ delay, duration: reduce ? 0 : 0.7, ease: EASE }}
                    />
                    {!reduce && <Spark path={n.path} delay={delay + 0.45} />}
                  </motion.g>
                )}
              </AnimatePresence>
            </g>
          )
        })}
      </svg>

      {/* Aurora */}
      <div
        className="absolute -translate-x-1/2 -translate-y-1/2"
        style={{ left: "50%", top: "50%", width: pct(80, W), minWidth: 56 }}
      >
        <motion.div
          className="relative aspect-square w-full overflow-hidden rounded-[22%] border border-white/10 bg-black shadow-[0_12px_48px_-12px_rgba(0,0,0,0.9)]"
          initial={reduce ? false : { opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: reduce ? 0 : 0.5, ease: EASE }}
        >
          <Image src="/arvologo.png" alt="Aurora" fill sizes="80px" className="object-cover" priority />
        </motion.div>
        <AnimatePresence>
          {allDone && !reduce && (
            <motion.span
              key="ready"
              className="pointer-events-none absolute inset-0 rounded-[22%]"
              style={{ boxShadow: "0 0 0 2px rgba(38,230,128,0.7), 0 0 40px 6px rgba(26,153,204,0.35)" }}
              initial={{ opacity: 0, scale: 1 }}
              animate={{ opacity: [0, 1, 0.55], scale: [1, 1.12, 1.06] }}
              transition={{ delay: 1.1, duration: 1.2, ease: "easeOut" }}
            />
          )}
        </AnimatePresence>
      </div>

      {STACK.map((p) => {
        const n = NODES[p.id]
        const ids = connectedIn(p, connected)
        const lit = ids.length > 0
        const first = lit ? connectorRegistry.get(ids[0]) : undefined
        const Icon = ICONS[p.id]
        const label = first ? first.name : p.short
        return (
          <button
            key={p.id}
            type="button"
            onClick={() => onSelect(p.id)}
            className="group absolute flex -translate-x-1/2 -translate-y-1/2 flex-col items-center rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 focus-visible:ring-offset-4 focus-visible:ring-offset-black"
            style={{ left: pct(n.x, W), top: pct(n.y, H), width: pct(60, W), minWidth: 40 }}
            aria-label={lit ? `${p.short}: ${ids.map((id) => connectorRegistry.get(id)?.name ?? id).join(", ")} connected` : `${p.short}: not connected yet`}
          >
            <AnimatePresence mode="wait" initial={false}>
              {lit ? (
                <motion.span
                  key="on"
                  className="relative flex aspect-square w-full items-center justify-center rounded-[26%] bg-white shadow-[0_8px_30px_-8px_rgba(38,230,128,0.45)]"
                  initial={reduce ? false : { scale: 0.6, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  exit={{ scale: 0.8, opacity: 0 }}
                  transition={{ type: "spring", stiffness: 420, damping: 22 }}
                >
                  {first?.iconPath ? (
                    <Image src={first.iconPath} alt="" width={28} height={28} className="h-1/2 w-1/2 object-contain" />
                  ) : (
                    <Icon className="h-1/2 w-1/2 text-black/70" />
                  )}
                  {ids.length > 1 && (
                    <span className="absolute -right-1.5 -top-1.5 rounded-full border border-black/80 bg-[#2eb67d] px-1.5 text-[10px] font-semibold leading-4 text-black tabular-nums">
                      +{ids.length - 1}
                    </span>
                  )}
                </motion.span>
              ) : (
                <motion.span
                  key="off"
                  className="flex aspect-square w-full items-center justify-center rounded-[26%] border border-dashed border-white/20 bg-black/40 backdrop-blur-sm transition-colors group-hover:border-white/45 group-hover:bg-white/[0.06]"
                  initial={reduce ? false : { opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.2 }}
                >
                  <Icon className="h-[38%] w-[38%] text-white/35 transition-colors group-hover:text-white/70" strokeWidth={1.75} />
                </motion.span>
              )}
            </AnimatePresence>
            <span className={`mt-2 max-w-[120px] truncate text-[11px] sm:text-xs ${lit ? "text-white/75" : "text-white/40 group-hover:text-white/70"}`}>
              {label}
            </span>
          </button>
        )
      })}
    </div>
  )
}

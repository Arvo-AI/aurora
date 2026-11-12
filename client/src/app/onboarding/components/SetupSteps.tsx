"use client"

import Image from "next/image"
import Link from "next/link"
import { useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import { STACK, SETUP_RETURN_KEY, connectHref, connectedIn, type StackPart, type StackPartId } from "@/lib/onboarding/stack"
import type { SetupGuide } from "@/hooks/use-setup-guide"

const GREEN = "#2eb67d"
const EASE = [0.22, 1, 0.36, 1] as const

export function Check({ done, reduce, size = 22 }: Readonly<{ done: boolean; reduce: boolean; size?: number }>) {
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" className="shrink-0" aria-hidden="true">
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

function rememberReturn() {
  try { sessionStorage.setItem(SETUP_RETURN_KEY, String(Date.now())) } catch { /* storage blocked: no way-back prompt */ }
}

function ProviderTile({ id, on }: Readonly<{ id: string; on: boolean }>) {
  const c = connectorRegistry.get(id)
  const [opening, setOpening] = useState(false)
  if (!c) return null

  const cls = `group flex w-full items-center gap-2.5 rounded-lg border px-3 py-2.5 text-left text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30 ${
    on
      ? "border-[#2eb67d]/40 bg-[#2eb67d]/[0.07] text-white"
      : "border-white/[0.08] bg-white/[0.02] text-white/80 hover:border-white/20 hover:bg-white/[0.05] hover:text-white"
  }`
  const body = (
    <>
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white p-1">
        {c.iconPath ? <Image src={c.iconPath} alt="" width={16} height={16} className="h-4 w-4 object-contain" /> : null}
      </span>
      <span className="min-w-0 flex-1 truncate">{opening ? "Opening…" : c.name}</span>
      {on && <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: GREEN }} aria-label="connected" />}
    </>
  )

  // Slack starts its OAuth straight from here instead of a detour through a card.
  if (id === "slack" && !on) {
    const start = async () => {
      setOpening(true)
      rememberReturn()
      try {
        const res = await fetch("/api/slack", { method: "POST" })
        const data = await res.json()
        if (data?.oauth_url) { globalThis.location.href = data.oauth_url; return }
      } catch { /* fall back to the card */ }
      globalThis.location.href = "/connectors?highlight=Slack"
    }
    return <button type="button" onClick={start} disabled={opening} className={cls}>{body}</button>
  }

  const href = on ? (c.path || `/connectors?highlight=${encodeURIComponent(c.name)}`) : connectHref(id, c.path)
  return <Link href={href} onClick={on ? undefined : rememberReturn} className={cls}>{body}</Link>
}

function StepRow({
  part, connected, open, onToggle, reduce, canConnect, orgName, guide,
}: Readonly<{
  part: StackPart; connected: ReadonlySet<string>; open: boolean; onToggle: () => void
  reduce: boolean; canConnect: boolean; orgName?: string; guide: SetupGuide | null
}>) {
  const ids = connectedIn(part, connected)
  const done = ids.length > 0
  const names = ids.map((id) => connectorRegistry.get(id)?.name ?? id)
  // Tiles: the featured ones, plus anything connected that isn't featured.
  const tiles = [...part.featured, ...ids.filter((id) => !part.featured.includes(id))]

  return (
    <li id={`setup-${part.id}`} className="scroll-mt-24 border-t border-white/[0.06] first:border-t-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full rounded-lg px-5 py-4 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-white/20"
      >
        <div className="flex w-full items-start gap-3.5">
          <Check done={done} reduce={reduce} />
          <div className="min-w-0 flex-1">
            <p className={`text-[15px] font-medium ${done ? "text-white/60" : "text-white"}`}>{part.title}</p>
            <p className="mt-0.5 text-[13px] leading-relaxed text-white/45">
              {done ? `Connected: ${names.join(", ")}.` : part.why}
            </p>
          </div>
          <motion.svg width="16" height="16" viewBox="0 0 24 24" className="mt-1 shrink-0 text-white/35" animate={{ rotate: open ? 180 : 0 }} transition={{ duration: reduce ? 0 : 0.25 }} aria-hidden="true">
            <path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </motion.svg>
        </div>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={{ duration: reduce ? 0 : 0.35, ease: EASE }}
            className="overflow-hidden"
          >
            <div className="px-5 pb-5 pl-[3.25rem]">
              {canConnect ? (
                <>
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    {tiles.map((id) => <ProviderTile key={id} id={id} on={connected.has(id)} />)}
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px]">
                    <Link href="/connectors" onClick={rememberReturn} className="text-white/55 underline decoration-white/20 underline-offset-4 hover:text-white hover:decoration-white/60">
                      See all connectors
                    </Link>
                    {guide?.booking_url && !done && (
                      <a href={guide.booking_url} target="_blank" rel="noopener noreferrer" className="text-white/55 underline decoration-white/20 underline-offset-4 hover:text-white hover:decoration-white/60">
                        Set it up with {guide.first_name} on a call
                      </a>
                    )}
                  </div>
                </>
              ) : (
                <p className="text-[13px] text-white/45">Ask an admin in {orgName ?? "your organization"} to connect this.</p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  )
}

export default function SetupSteps({
  connected, openStep, onToggle, reduce, canConnect, orgName, guide,
}: Readonly<{
  connected: ReadonlySet<string>; openStep: StackPartId | null; onToggle: (id: StackPartId) => void
  reduce: boolean; canConnect: boolean; orgName?: string; guide: SetupGuide | null
}>) {
  const doneCount = STACK.filter((p) => connectedIn(p, connected).length > 0).length
  return (
    <section aria-labelledby="setup-heading" className="rounded-2xl border border-white/[0.08] bg-black/45 backdrop-blur-md">
      <div className="flex items-center justify-between gap-4 px-5 pb-3 pt-5">
        <h2 id="setup-heading" className="text-[15px] font-medium text-white">Connect your stack</h2>
        <div className="flex shrink-0 items-center gap-2" aria-label={`${doneCount} of ${STACK.length} connected`}>
          <div className="flex gap-1">
            {STACK.map((p, i) => (
              <motion.span key={p.id} className="h-1 w-5 rounded-full" initial={false}
                animate={{ backgroundColor: i < doneCount ? GREEN : "rgba(255,255,255,0.12)" }}
                transition={{ duration: reduce ? 0 : 0.4, delay: reduce ? 0 : i * 0.05 }} />
            ))}
          </div>
          <span className="whitespace-nowrap text-xs tabular-nums text-white/45">{doneCount} of {STACK.length}</span>
        </div>
      </div>
      <ol className="pb-1">
        {STACK.map((part) => (
          <StepRow
            key={part.id}
            part={part}
            connected={connected}
            open={openStep === part.id}
            onToggle={() => onToggle(part.id)}
            reduce={reduce}
            canConnect={canConnect}
            orgName={orgName}
            guide={guide}
          />
        ))}
      </ol>
    </section>
  )
}

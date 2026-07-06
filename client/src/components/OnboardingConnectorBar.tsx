"use client"

import { useSearchParams } from "next/navigation"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import { ChevronRight, ChevronUp } from "lucide-react"
import { Suspense, useEffect, useState } from "react"

const ONBOARDING_QUEUE_KEY = "aurora_onboarding_queue"

interface OnboardingQueueState {
  queue: string[]
  current: number
}

function getConnectorUrl(id: string, queue: string[], index: number): string {
  const connector = connectorRegistry.get(id)
  const name = connector?.name || id
  const queueParam = `onboarding=1&queue=${queue.join(",")}&current=${index}`
  return `/connectors?${queueParam}&highlight=${encodeURIComponent(name)}`
}

function parseStoredState(stored: string): OnboardingQueueState | null {
  try {
    const parsed = JSON.parse(stored) as Partial<OnboardingQueueState>
    if (!Array.isArray(parsed.queue)) return null
    const sanitizedQueue = parsed.queue.filter(
      (item): item is string => typeof item === "string" && item.length > 0
    )
    if (sanitizedQueue.length === 0) return null
    const safeCurrent = Number.isInteger(parsed.current)
      ? Math.min(Math.max(parsed.current!, 0), sanitizedQueue.length - 1)
      : 0
    return { queue: sanitizedQueue, current: safeCurrent }
  } catch {
    return null
  }
}

function OnboardingConnectorBarInner() {
  const searchParams = useSearchParams()
  const [state, setState] = useState<OnboardingQueueState | null>(null)
  const [expanded, setExpanded] = useState(true)

  const urlOnboarding = searchParams.get("onboarding") === "1"
  const urlQueue = (searchParams.get("queue")?.split(",") || []).filter(Boolean)
  const parsedUrlCurrent = Number.parseInt(searchParams.get("current") || "0", 10)

  useEffect(() => {
    if (urlOnboarding && urlQueue.length > 0) {
      const safeCurrent = Number.isFinite(parsedUrlCurrent)
        ? Math.min(Math.max(parsedUrlCurrent, 0), urlQueue.length - 1)
        : 0
      const newState = { queue: urlQueue, current: safeCurrent }
      sessionStorage.setItem(ONBOARDING_QUEUE_KEY, JSON.stringify(newState))
      setState(newState)
    } else {
      const stored = sessionStorage.getItem(ONBOARDING_QUEUE_KEY)
      if (stored) {
        const parsed = parseStoredState(stored)
        if (parsed) {
          setState(parsed)
        } else {
          sessionStorage.removeItem(ONBOARDING_QUEUE_KEY)
        }
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!state || state.queue.length === 0) return null

  const { queue, current } = state
  const currentConnector = connectorRegistry.get(queue[current])
  const isLast = current >= queue.length - 1
  const nextConnector = isLast ? null : connectorRegistry.get(queue[current + 1])

  const handleNext = () => {
    if (isLast) {
      sessionStorage.removeItem(ONBOARDING_QUEUE_KEY)
      globalThis.location.href = "/"
    } else {
      const newState = { queue, current: current + 1 }
      sessionStorage.setItem(ONBOARDING_QUEUE_KEY, JSON.stringify(newState))
      globalThis.location.href = getConnectorUrl(queue[current + 1], queue, current + 1)
    }
  }

  const handleBack = () => {
    if (current > 0) {
      const newState = { queue, current: current - 1 }
      sessionStorage.setItem(ONBOARDING_QUEUE_KEY, JSON.stringify(newState))
      globalThis.location.href = getConnectorUrl(queue[current - 1], queue, current - 1)
    }
  }

  const handleDismiss = () => {
    sessionStorage.removeItem(ONBOARDING_QUEUE_KEY)
    setState(null)
  }

  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-50">
      {expanded ? (
        <div className="bg-gray-900/95 backdrop-blur-md border border-white/10 rounded-xl px-5 py-2.5 shadow-2xl flex items-center gap-4 animate-in fade-in slide-in-from-bottom-2 duration-200">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-16 h-1.5 rounded-full bg-white/10 overflow-hidden flex-shrink-0">
              <div
                className="h-full rounded-full bg-emerald-400 transition-all duration-300"
                style={{ width: `${((current + 1) / queue.length) * 100}%` }}
              />
            </div>
            <span className="text-sm text-white/70 truncate">
              Setting up <span className="text-white font-medium">{currentConnector?.name || queue[current]}</span>
              <span className="text-white/40 ml-1">({current + 1}/{queue.length})</span>
            </span>
          </div>

          <div className="flex items-center gap-1 flex-shrink-0">
            {current > 0 && (
              <button
                onClick={handleBack}
                className="px-3 py-1.5 text-xs text-white/50 hover:text-white/80 transition-colors rounded-lg hover:bg-white/5"
              >
                Back
              </button>
            )}
            {!isLast && (
              <button
                onClick={handleDismiss}
                className="px-3 py-1.5 text-xs text-white/50 hover:text-white/80 transition-colors rounded-lg hover:bg-white/5"
              >
                Skip All
              </button>
            )}
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={handleNext}
              className="flex items-center gap-1.5 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium rounded-lg transition-colors"
            >
              {isLast ? "Finish Setup" : `Next: ${nextConnector?.name || queue[current + 1]}`}
              <ChevronRight className="w-4 h-4" />
            </button>
            <button
              onClick={() => setExpanded(false)}
              className="ml-1 p-1 text-white/30 hover:text-white/60 transition-colors"
            >
              <ChevronUp className="w-4 h-4 rotate-180" />
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setExpanded(true)}
          className="bg-gray-900/90 backdrop-blur-md border border-white/10 rounded-full px-4 py-2 shadow-2xl flex items-center gap-2.5 hover:bg-gray-800/95 transition-colors"
        >
          <div className="w-12 h-1.5 rounded-full bg-white/10 overflow-hidden">
            <div
              className="h-full rounded-full bg-emerald-400 transition-all duration-300"
              style={{ width: `${((current + 1) / queue.length) * 100}%` }}
            />
          </div>
          <span className="text-xs text-white/70">
            Setup {current + 1}/{queue.length}
          </span>
          <ChevronUp className="w-3 h-3 text-white/40" />
        </button>
      )}
    </div>
  )
}

export default function OnboardingConnectorBar() {
  return (
    <Suspense fallback={null}>
      <OnboardingConnectorBarInner />
    </Suspense>
  )
}

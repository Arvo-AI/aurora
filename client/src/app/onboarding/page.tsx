"use client"

import { useOnboarding } from "./components/OnboardingContext"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import ProgressBar from "./components/ProgressBar"
import ConnectorTile from "./components/ConnectorTile"
import Image from "next/image"
import { useState, useMemo } from "react"
import { motion, AnimatePresence } from "framer-motion"

export default function OnboardingPage() {
  const {
    state,
    step,
    totalSteps,
    goNext,
    goBack,
    addSelection,
    removeSelection,
    getSelectedConnectors,
  } = useOnboarding()
  const [isFinishing, setIsFinishing] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [showConfirmDialog, setShowConfirmDialog] = useState(false)
  const [missingCategories, setMissingCategories] = useState<string[]>([])

  const actualDevToolIds = useMemo(
    () => connectorRegistry.getByCategory("Development").map(c => c.id),
    []
  )

  const checkMissingCategories = () => {
    const missing: string[] = []
    if (state.selections.alerting.length === 0) missing.push("Alerting Platform")
    if (state.selections.infrastructure.length === 0) missing.push("Infrastructure")
    const hasDevTool = state.selections.development.some(id => actualDevToolIds.includes(id))
    if (!hasDevTool) missing.push("Development Platform")
    return missing
  }

  const handleFinishAttempt = (skip = false) => {
    if (skip) {
      doFinish(true)
      return
    }
    const missing = checkMissingCategories()
    if (missing.length > 0) {
      setMissingCategories(missing)
      setShowConfirmDialog(true)
    } else {
      doFinish(false)
    }
  }

  const doFinish = async (skip = false) => {
    setIsFinishing(true)
    setErrorMessage("")
    const selectedIds = skip ? [] : getSelectedConnectors()
    const controller = new AbortController()
    const timeoutId = globalThis.setTimeout(() => controller.abort(), 15_000)
    try {
      const res = await fetch("/api/onboarding/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_connectors: selectedIds }),
        signal: controller.signal,
      })
      if (!res.ok) {
        throw new Error(`Onboarding completion failed: ${res.status}`)
      }
    } catch (e) {
      console.error("Finish onboarding error:", e)
      setErrorMessage(
        e instanceof Error && e.name === "AbortError"
          ? "Request timed out. Please try again."
          : "Something went wrong. Please try again."
      )
      setIsFinishing(false)
      return
    } finally {
      globalThis.clearTimeout(timeoutId)
    }

    if (!skip && selectedIds.length > 0) {
      const firstConnector = connectorRegistry.get(selectedIds[0])
      const name = firstConnector?.name || selectedIds[0]
      const params = new URLSearchParams({
        onboarding: "1",
        queue: selectedIds.join(","),
        current: "0",
        highlight: name,
      })
      globalThis.location.href = `/connectors?${params.toString()}`
    } else {
      globalThis.location.href = "/"
    }
  }

  const isSelected = (id: string) => {
    return Object.values(state.selections).some(arr => arr.includes(id))
  }

  const toggle = (page: keyof typeof state.selections, id: string) => {
    if (isSelected(id)) {
      for (const key of Object.keys(state.selections) as Array<keyof typeof state.selections>) {
        if (state.selections[key].includes(id)) {
          removeSelection(key, id)
        }
      }
    } else {
      addSelection(page, id)
    }
  }

  const monitoringConnectors = useMemo(() => connectorRegistry.getByCategory("Monitoring"), [])
  const infraConnectors = useMemo(() => [
    ...connectorRegistry.getByCategory("Infrastructure"),
    ...connectorRegistry.getByCategory("Networking"),
  ], [])
  const alertingConnectors = useMemo(() => [
    ...connectorRegistry.getByCategory("Incident Management"),
    ...connectorRegistry.getByCategory("Monitoring"),
  ], [])
  const communicationConnectors = useMemo(() => [
    ...connectorRegistry.getByCategory("Communication"),
  ], [])
  const devConnectors = useMemo(() => [
    ...connectorRegistry.getByCategory("Development"),
    ...connectorRegistry.getByCategory("CI/CD"),
    ...connectorRegistry.getByCategory("Documentation"),
  ], [])

  const selectedIds = getSelectedConnectors()
  const selectedConnectors = selectedIds
    .map((id) => connectorRegistry.get(id))
    .filter(Boolean)

  let finishButtonLabel = "Finish"
  if (isFinishing) finishButtonLabel = "Setting up..."
  else if (selectedConnectors.length > 0) finishButtonLabel = "Start Configuration"

  return (
    <div className="h-screen flex flex-col">
      <div className="px-6 pt-6 pb-2 max-w-[640px] mx-auto w-full">
        <ProgressBar step={step} totalVisible={totalSteps - 1} />
      </div>

      <div className="flex-1 overflow-hidden min-h-0">
        <div
          className="h-full flex transition-transform duration-500 ease-[cubic-bezier(0.4,0,0.15,1)]"
          style={{ transform: `translateX(-${step * 100}%)` }}
        >
          {/* Step 0: Welcome */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-8">
              <div className="flex items-center gap-4">
                <Image src="/arvologo.png" alt="Aurora" width={48} height={48} className="rounded-xl" />
                <span className="text-white font-bold text-xl">Aurora</span>
              </div>
              <div>
                <h1 className="text-3xl font-bold text-white">Incident response,</h1>
                <h1 className="text-3xl font-bold text-[#3fa266]">automated.</h1>
              </div>
              <p className="text-[#ccc] text-sm leading-relaxed">
                Aurora is your AI-powered incident response platform. It monitors your
                infrastructure, detects anomalies, runs root cause analysis, and helps
                your team resolve incidents faster — automatically.
              </p>
              <p className="text-[#888] text-xs leading-relaxed">
                Let&apos;s connect your tools so Aurora can start working for your team
                 — you can always change things later.
              </p>
            </div>
          </div>

          {/* Step 1: Alerting Platforms */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Which alerting platforms should trigger Aurora?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  These tools deliver webhooks to Aurora when incidents occur. Aurora uses them to kick off root cause analysis automatically.
                </p>
                <div className="mt-3 rounded-lg bg-amber-500/15 border border-amber-500/40 px-4 py-3.5 flex items-start gap-3">
                  <svg className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
                  </svg>
                  <p className="text-sm text-amber-200/90 leading-relaxed">
                    <span className="font-semibold text-amber-300">Important:</span> Without at least one alerting platform connected, Aurora won&apos;t receive alerts and cannot perform automated root cause analysis.
                  </p>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {alertingConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={isSelected(c.id)}
                    onToggle={() => toggle("alerting", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 2: Monitoring */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Which monitoring tools does your team use?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Aurora pulls alerts, metrics, and logs from your monitoring stack to power root cause analysis.
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {monitoringConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={isSelected(c.id)}
                    onToggle={() => toggle("monitoring", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 3: Infrastructure */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Which cloud providers and infrastructure tools do you use?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Connect your infrastructure so Aurora can investigate resources during incidents and correlate deployment changes.
                </p>
                <div className="mt-3 rounded-lg bg-amber-500/15 border border-amber-500/40 px-4 py-3.5 flex items-start gap-3">
                  <svg className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
                  </svg>
                  <p className="text-sm text-amber-200/90 leading-relaxed">
                    <span className="font-semibold text-amber-300">Important:</span> Without at least one infrastructure connection, Aurora cannot access logs or investigate cloud resources during incidents.
                  </p>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {infraConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={isSelected(c.id)}
                    onToggle={() => toggle("infrastructure", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 4: Development and CI/CD */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Which development and documentation tools do you use?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Aurora uses your repos, CI pipelines, and docs for richer context during root cause analysis.
                </p>
                <div className="mt-3 rounded-lg bg-amber-500/15 border border-amber-500/40 px-4 py-3.5 flex items-start gap-3">
                  <svg className="w-5 h-5 text-amber-400 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
                  </svg>
                  <p className="text-sm text-amber-200/90 leading-relaxed">
                    <span className="font-semibold text-amber-300">Important:</span> Without a connected codebase, Aurora cannot correlate code changes with incidents or investigate recent deployments.
                  </p>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {devConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={isSelected(c.id)}
                    onToggle={() => toggle("development", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 5: Communication Channels */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Where should Aurora send updates during incidents?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Communication channels let Aurora post status updates, RCA findings, and notifications to your team in real time.
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {communicationConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={isSelected(c.id)}
                    onToggle={() => toggle("communication", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 6: Book a Meeting */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  We&apos;d love to connect with you.
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Book a quick 15-minute call with our team. We&apos;ll help you get the most out of Aurora
                  and answer any questions about your setup.
                </p>
              </div>
              <a
                href="https://cal.com/arvo-ai"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-5 py-3 bg-white text-black font-medium text-sm rounded-lg hover:bg-white/90 transition-colors"
              >
                Book a meeting &rarr;
              </a>
              <p className="text-xs text-[#666]">
                No pressure — this is completely optional.
              </p>
            </div>
          </div>

          {/* Step 7: Review and Connect */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  {selectedConnectors.length > 0 ? "Great choices! Let's get them connected." : "Review your setup"}
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  {selectedConnectors.length > 0
                    ? "We'll walk you through configuring each connector. Most take under a minute."
                    : "You haven't selected any connectors yet. You can always add them later from Settings."}
                </p>
              </div>


              {selectedConnectors.length > 0 ? (
                <div className="space-y-2">
                  {selectedConnectors.map((c, idx) => (
                    <motion.div
                      key={c!.id}
                      initial={{ opacity: 0, x: 12 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ duration: 0.25, delay: idx * 0.05 }}
                      className="flex items-center gap-3 px-4 py-3 rounded-lg border border-white/[0.08] bg-white/[0.04] backdrop-blur-sm"
                    >
                      <span className="flex-shrink-0 w-6 h-6 rounded-full bg-white/[0.08] flex items-center justify-center text-xs text-[#aaa] font-medium">
                        {idx + 1}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm text-white font-medium">{c!.name}</p>
                        <p className="text-xs text-[#777]">{c!.category}</p>
                      </div>
                    </motion.div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-4">
                  <p className="text-sm text-[#888]">
                    No connectors selected. You can always add them later from Settings.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Gradient fade above bottom nav */}
      <div className="fixed bottom-0 inset-x-0 z-10 h-28 pointer-events-none bg-gradient-to-t from-black/90 via-black/60 to-transparent" />

      {/* Fixed bottom nav */}
      <div className="fixed bottom-0 inset-x-0 z-20 bg-black/80 backdrop-blur-md border-t border-white/[0.06]">
        <div className="max-w-[640px] mx-auto px-6 py-5 flex flex-col gap-3 relative">
          {errorMessage && (
            <div role="alert" aria-live="polite" className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3">
              <p className="text-sm text-red-400">{errorMessage}</p>
            </div>
          )}
          <div className="flex items-center justify-between">
          <div>
            {step > 0 && (
              <button
                onClick={goBack}
                className="px-4 py-2 text-sm text-[#aaa] border border-white/[0.12] rounded-lg hover:text-white hover:border-white/25 backdrop-blur-sm bg-black/20 transition-colors"
              >
                Back
              </button>
            )}
          </div>

          {step < totalSteps - 1 && (
            <button
              onClick={() => handleFinishAttempt(true)}
              disabled={isFinishing}
              className="absolute left-1/2 -translate-x-1/2 px-3 py-2 text-xs text-[#666] hover:text-[#999] transition-colors disabled:opacity-50"
            >
              {isFinishing ? "Skipping..." : "Skip"}
            </button>
          )}

          <div>
            {step < totalSteps - 1 ? (
              <button
                onClick={goNext}
                className="px-5 py-2.5 text-sm font-medium bg-white text-black rounded-lg hover:bg-white/90 active:scale-[0.97] transition-all"
              >
                Continue
              </button>
            ) : (
              <button
                onClick={() => handleFinishAttempt()}
                disabled={isFinishing}
                className="px-5 py-2.5 text-sm font-medium bg-white text-black rounded-lg hover:bg-white/90 active:scale-[0.97] transition-all disabled:opacity-50"
              >
                {finishButtonLabel}
              </button>
            )}
          </div>
          </div>
        </div>
      </div>

      {/* Confirmation dialog for missing platform categories */}
      <AnimatePresence>
        {showConfirmDialog && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          >
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 10 }}
              transition={{ duration: 0.2 }}
              className="w-full max-w-md mx-4 rounded-xl border border-amber-500/30 bg-[#1a1a1a] p-6 shadow-2xl"
            >
              <div className="flex items-start gap-3 mb-4">
                <div className="flex-shrink-0 w-10 h-10 rounded-full bg-amber-500/10 flex items-center justify-center">
                  <svg className="w-5 h-5 text-amber-400" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
                  </svg>
                </div>
                <div>
                  <h3 className="text-base font-semibold text-white">Missing platform connections</h3>
                  <p className="text-sm text-[#aaa] mt-1">
                    You haven&apos;t selected any connectors for the following:
                  </p>
                </div>
              </div>

              <div className="space-y-2 mb-5">
                {missingCategories.map((cat) => (
                  <div key={cat} className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-500/5 border border-amber-500/15">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0" />
                    <span className="text-sm text-amber-200/90">{cat}</span>
                  </div>
                ))}
              </div>

              <p className="text-xs text-[#888] mb-5 leading-relaxed">
                Without these connections, Aurora&apos;s capabilities will be significantly limited.
                {missingCategories.includes("Alerting Platform") && " Without an alerting platform, Aurora cannot receive incidents or trigger automated root cause analysis."}
                {missingCategories.includes("Infrastructure") && " Without infrastructure access, Aurora cannot check logs or investigate cloud resources during incidents."}
                {missingCategories.includes("Development Platform") && " Without a connected codebase, Aurora cannot correlate code changes with incidents."}
              </p>

              <div className="flex items-center gap-3">
                <button
                  onClick={() => setShowConfirmDialog(false)}
                  className="flex-1 px-4 py-2.5 text-sm font-medium text-white border border-white/[0.15] rounded-lg hover:border-white/30 hover:bg-white/[0.05] transition-colors"
                >
                  Go back &amp; connect
                </button>
                <button
                  onClick={() => {
                    setShowConfirmDialog(false)
                    doFinish(false)
                  }}
                  className="flex-1 px-4 py-2.5 text-sm font-medium text-amber-200/80 border border-amber-500/20 rounded-lg hover:bg-amber-500/10 transition-colors"
                >
                  Continue anyway
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

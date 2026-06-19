"use client"

import { useOnboarding } from "./components/OnboardingContext"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"
import ProgressBar from "./components/ProgressBar"
import ConnectorTile from "./components/ConnectorTile"
import Image from "next/image"
import { useState } from "react"
import { motion } from "framer-motion"

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

  const handleFinish = async (skip = false) => {
    setIsFinishing(true)
    const selectedIds = skip ? [] : getSelectedConnectors()
    try {
      await fetch("/api/onboarding/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selected_connectors: selectedIds }),
      })
    } catch (e) {
      console.error("Finish onboarding error:", e)
    }

    if (!skip && selectedIds.length > 0) {
      const firstConnector = connectorRegistry.get(selectedIds[0])
      const name = firstConnector?.name || selectedIds[0]
      const queueParam = `onboarding=1&queue=${selectedIds.join(",")}&current=0`
      window.location.href = `/connectors?${queueParam}&highlight=${encodeURIComponent(name)}`
    } else {
      window.location.href = "/"
    }
  }

  const toggle = (page: keyof typeof state.selections, id: string) => {
    if (state.selections[page].includes(id)) {
      removeSelection(page, id)
    } else {
      addSelection(page, id)
    }
  }

  const monitoringConnectors = connectorRegistry.getByCategory("Monitoring")
  const infraConnectors = [
    ...connectorRegistry.getByCategory("Infrastructure"),
    ...connectorRegistry.getByCategory("Networking"),
  ]
  const alertingConnectors = [
    ...connectorRegistry.getByCategory("Incident Management"),
    ...connectorRegistry.getByCategory("Communication"),
  ]
  const devConnectors = [
    ...connectorRegistry.getByCategory("Development"),
    ...connectorRegistry.getByCategory("CI/CD"),
    ...connectorRegistry.getByCategory("Documentation"),
  ]

  const selectedIds = getSelectedConnectors()
  const selectedConnectors = selectedIds
    .map((id) => connectorRegistry.get(id))
    .filter(Boolean)

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

          {/* Step 1: Monitoring */}
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
                    selected={state.selections.monitoring.includes(c.id)}
                    onToggle={() => toggle("monitoring", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 2: Infrastructure */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Which cloud providers and infrastructure tools do you use?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Connect your infrastructure so Aurora can investigate resources during incidents and correlate deployment changes.
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {infraConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={state.selections.infrastructure.includes(c.id)}
                    onToggle={() => toggle("infrastructure", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 3: Alerting and Communication */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Where do your alerts and team notifications go?
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  Aurora can receive alerts from your incident management tools and send updates to your team&apos;s communication channels.
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {alertingConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={state.selections.alerting.includes(c.id)}
                    onToggle={() => toggle("alerting", c.id)}
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
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {devConnectors.map((c) => (
                  <ConnectorTile
                    key={c.id}
                    connector={c}
                    selected={state.selections.development.includes(c.id)}
                    onToggle={() => toggle("development", c.id)}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Step 5: Book a Meeting */}
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
            </div>
          </div>

          {/* Step 6: Review and Connect */}
          <div className="w-full flex-shrink-0 flex items-start justify-center px-6 pt-6 pb-24 overflow-y-auto hide-scrollbar">
            <div className="w-full max-w-[640px] space-y-6">
              <div>
                <h2 className="text-xl font-semibold text-white">
                  Great choices! Let&apos;s get them connected.
                </h2>
                <p className="text-sm text-[#aaa] mt-1.5">
                  We&apos;ll walk you through configuring each connector. Most take under a minute.
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
                <div className="text-center py-8">
                  <p className="text-sm text-[#888]">
                    No connectors selected. You can always add them later from Settings.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Fixed bottom nav */}
      <div className="fixed bottom-0 inset-x-0 z-20">
        <div className="max-w-[640px] mx-auto px-6 py-5 flex items-center justify-between relative">
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
              onClick={() => handleFinish(true)}
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
                onClick={() => handleFinish()}
                disabled={isFinishing}
                className="px-5 py-2.5 text-sm font-medium bg-white text-black rounded-lg hover:bg-white/90 active:scale-[0.97] transition-all disabled:opacity-50"
              >
                {isFinishing ? "Setting up..." : selectedConnectors.length > 0 ? "Start Configuration" : "Finish"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

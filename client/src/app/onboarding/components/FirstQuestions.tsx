"use client"

import Image from "next/image"
import Link from "next/link"
import { connectorRegistry } from "@/components/connectors/ConnectorRegistry"

// The first useful moment shouldn't wait for an incident. Once Aurora can see
// something real, offer a question about it; one click asks it in a new chat.
// Read-only ("ask") mode, so a first try can't change anything.

interface Question {
  provider: string
  text: string
}

const CLOUD_NAMES: Record<string, string> = { aws: "AWS account", gcp: "Google Cloud project", azure: "Azure subscription" }

function questionsFor(connected: ReadonlySet<string>): Question[] {
  const out: Question[] = []
  const cloud = ["aws", "gcp", "azure"].find((id) => connected.has(id))
  if (cloud) out.push({ provider: cloud, text: `What's running in my ${CLOUD_NAMES[cloud]}, and does anything look unhealthy?` })
  if (connected.has("kubectl")) out.push({ provider: "kubectl", text: "Are any pods crash-looping or restarting a lot in my cluster?" })
  const alerts = ["datadog", "grafana", "newrelic", "pagerduty", "opsgenie", "sentry"].find((id) => connected.has(id))
  if (alerts) {
    const name = connectorRegistry.get(alerts)?.name ?? alerts
    out.push({ provider: alerts, text: `Summarize what ${name} alerted on in the last 24 hours.` })
  }
  const code = ["github", "gitlab", "bitbucket"].find((id) => connected.has(id))
  if (code) out.push({ provider: code, text: "What changed in my repos this week that could cause an incident?" })
  return out.slice(0, 3)
}

export default function FirstQuestions({ connected }: Readonly<{ connected: ReadonlySet<string> }>) {
  const questions = questionsFor(connected)
  if (questions.length === 0) return null
  return (
    <section aria-labelledby="first-questions-heading" className="rounded-2xl border border-white/[0.08] bg-black/45 px-5 pb-3 pt-5 backdrop-blur-md">
      <h2 id="first-questions-heading" className="text-[15px] font-medium text-white">Ask Aurora something real</h2>
      <p className="mt-0.5 text-[13px] text-white/45">It can already see enough to answer these. Nothing gets changed.</p>
      <ul className="mt-3">
        {questions.map((q) => {
          const icon = connectorRegistry.get(q.provider)?.iconPath
          return (
            <li key={q.text} className="border-t border-white/[0.06] first:border-t-0">
              <Link
                href={`/chat?newChat=true&mode=ask&message=${encodeURIComponent(q.text)}`}
                className="group -mx-2 flex items-center gap-3 rounded-lg px-2 py-3 text-[14px] text-white/80 transition-colors hover:bg-white/[0.04] hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/25"
              >
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-white p-1">
                  {icon ? <Image src={icon} alt="" width={16} height={16} className="h-4 w-4 object-contain" /> : null}
                </span>
                <span className="min-w-0 flex-1">{q.text}</span>
                <span className="shrink-0 text-[13px] text-white/35 transition-colors group-hover:text-white/70">Ask</span>
              </Link>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

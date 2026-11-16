// The four places Aurora needs to see into, in the order they pay off.
// Provider ids match /api/connectors/status keys and the server-side
// services/onboarding/stack.py sets; keep the two in sync.

export type StackPartId = "alerts" | "cloud" | "code" | "chat"

export interface StackPart {
  id: StackPartId
  title: string
  // What Aurora can do once this part is connected, in the user's terms.
  why: string
  // Shown in the setup map when nothing is connected yet.
  short: string
  // The first few offered as tiles; everything else is on /connectors.
  featured: string[]
  all: ReadonlySet<string>
}

export const STACK: readonly StackPart[] = [
  {
    id: "alerts",
    title: "Connect where your alerts come from",
    why: "Aurora starts investigating the moment an alert fires.",
    short: "Alerts",
    featured: ["pagerduty", "datadog", "grafana", "opsgenie", "incidentio", "newrelic"],
    all: new Set([
      "pagerduty", "opsgenie", "incidentio", "servicenow", "bigpanda", "datadog", "grafana",
      "newrelic", "dynatrace", "sentry", "splunk", "elastic", "coroot", "netdata", "cloudwatch",
      "thousandeyes",
    ]),
  },
  {
    id: "cloud",
    title: "Connect where your services run",
    why: "Aurora can then read the logs, metrics and resources behind each alert.",
    short: "Cloud",
    featured: ["aws", "gcp", "azure", "kubectl", "cloudflare", "onprem"],
    all: new Set(["aws", "gcp", "azure", "kubectl", "onprem", "cloudflare", "flyio", "ovh", "scaleway", "tailscale"]),
  },
  {
    id: "code",
    title: "Connect your code",
    why: "Aurora can then tie an incident to the deploy or commit that caused it.",
    short: "Code",
    featured: ["github", "gitlab", "bitbucket"],
    all: new Set(["github", "gitlab", "bitbucket"]),
  },
  {
    id: "chat",
    title: "Bring Aurora into your team chat",
    why: "Aurora posts what it found where your team already talks.",
    short: "Chat",
    featured: ["slack", "teams", "google_chat"],
    all: new Set(["slack", "teams", "google_chat"]),
  },
]

export function partOf(provider: string): StackPart | undefined {
  return STACK.find((p) => p.all.has(provider))
}

export function connectedIn(part: StackPart, connected: ReadonlySet<string>): string[] {
  // Featured first so the map shows the tool the user most likely just connected.
  const ids = [...connected].filter((id) => part.all.has(id))
  return ids.sort((a, b) => rank(part, a) - rank(part, b))
}

function rank(part: StackPart, id: string): number {
  const i = part.featured.indexOf(id)
  return i === -1 ? 99 : i
}

export function progress(connected: ReadonlySet<string>): { done: number; total: number; next: StackPart | null } {
  const doneParts = STACK.filter((p) => connectedIn(p, connected).length > 0)
  const next = STACK.find((p) => connectedIn(p, connected).length === 0) ?? null
  return { done: doneParts.length, total: STACK.length, next }
}

// Where a tile sends someone to connect. Most connectors have their own page;
// these connect from their card on /connectors or start OAuth directly.
const SPECIAL_HREFS: Record<string, string> = {
  gcp: "/gcp/auth",
  gitlab: "/gitlab/connect",
  github: "/connectors?highlight=GitHub&connect=github",
  teams: "/connectors?highlight=Microsoft%20Teams",
  google_chat: "/connectors?highlight=Google%20Chat",
}

export function connectHref(id: string, registryPath: string | undefined): string {
  return SPECIAL_HREFS[id] || registryPath || `/connectors?highlight=${encodeURIComponent(id)}`
}

// Set when someone leaves the setup page to connect a tool, so the app can
// offer the way back once the connection lands.
export const SETUP_RETURN_KEY = "aurora_setup_return"

import { STACK, connectedIn, type StackPart } from "@/lib/onboarding/stack"

// The setup page's headline and lede, from where the account is.

export interface SetupWords {
  headline: string
  lede: string
}

interface Situation {
  first: string | null
  connected: ReadonlySet<string>
  next: StackPart | null
  via: string | null
  joined: boolean
  slackFailed: boolean
  orgName?: string
}

function joinWords(words: string[]): string {
  if (words.length <= 1) return words.join("")
  return `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`
}

function lowerFirst(s: string): string {
  return s.charAt(0).toLowerCase() + s.slice(1)
}

function slackWords(s: Situation): SetupWords | null {
  if (s.via !== "slack") return null
  if (s.joined) {
    return {
      headline: s.orgName ? `You're in ${s.orgName}` : "You're in",
      lede: "Your team already runs Aurora. Mention @Aurora in any Slack channel to ask about an incident.",
    }
  }
  if (s.slackFailed) {
    return {
      headline: "Your account is ready",
      lede: "Aurora couldn't create its incidents channel in Slack. Check that the app is allowed to create channels, then try again.",
    }
  }
  return null
}

export function describeSetup(s: Situation): SetupWords {
  const special = slackWords(s)
  if (special) return special

  const done = STACK.filter((p) => connectedIn(p, s.connected).length > 0)
  if (!s.next) {
    return {
      headline: "Aurora can see your whole stack",
      lede: "The next alert that fires gets a full investigation, posted where your team already talks. You can add more tools from Connectors any time.",
    }
  }
  if (done.length === 0) {
    return {
      headline: s.first ? `Welcome to Aurora, ${s.first}` : "Welcome to Aurora",
      lede: "Connect where your alerts come from, where your services run and your code. Then Aurora can investigate your next incident on its own.",
    }
  }
  const nextStep = lowerFirst(s.next.title)
  const githubOnly = s.via === "github" && s.connected.has("github") && done.length === 1
  return {
    headline: `Aurora can see your ${joinWords(done.map((p) => p.short.toLowerCase()))}`,
    lede: githubOnly
      ? `Aurora is already reviewing pull requests on the repos you picked. Next, ${nextStep}: ${s.next.why}`
      : `Next, ${nextStep}. ${s.next.why}`,
  }
}

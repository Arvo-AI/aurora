"use client"

import { useEffect, useState } from "react"
import Image from "next/image"
import { motion, useReducedMotion } from "framer-motion"

// "Continue with Slack", the Slack variant of the handoff panel, and the
// messages for ?error=slack_* codes set by the backend's Slack flows.

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || ""
const GREEN = "#2eb67d"
const EASE = [0.22, 1, 0.36, 1] as const

export const SLACK_RESUME_KEY = "aurora_resume_slack_connect"

// Fixed strings only: the query param picks a message, it is never rendered.
export const SLACK_ERRORS: Record<string, string> = {
  slack_account_exists: "Your Slack email already has an Aurora account. Sign in below and we'll finish adding Aurora to Slack.",
  slack_workspace_not_connected: "Your Slack workspace doesn't use Aurora yet. Add it to Slack to create your team's account.",
  slack_not_member: "Only full members of the workspace can sign in with Slack. Guests and Slack Connect users need an invite.",
  slack_sso_required: "Your organization requires single sign-on. Sign in with SSO below.",
  slack_admin_password: "Admins sign in with their password or SSO. Sign in below; Slack works for everyone else on your team.",
  slack_identity_mismatch: "This Aurora account is linked to a different Slack user. Sign in with your password instead.",
  slack_identity: "Slack didn't confirm a verified email for your account. Try again, or sign in with your password.",
  slack_email_unconfirmed: "Slack hasn't confirmed the email on your account yet. Confirm it in Slack, then try again.",
  slack_expired: "That Slack link has expired or was opened in a different browser. Start again below.",
  slack_cancelled: "Slack sign-in was cancelled.",
  slack_unavailable: "Sign in with Slack isn't available on this deployment.",
  slack_internal: "Something went wrong signing in with Slack. Please try again.",
  handoff_failed: "Your account is ready, but signing you in failed. Use Continue with Slack below.",
}

/** After a password sign-in that started from "Add to Slack", go finish the install. */
export async function resumeSlackConnect(): Promise<boolean> {
  if (sessionStorage.getItem(SLACK_RESUME_KEY) !== "1") return false
  sessionStorage.removeItem(SLACK_RESUME_KEY)
  try {
    const res = await fetch("/api/slack", { method: "POST" })
    const data = await res.json()
    if (typeof data?.oauth_url === "string" && data.oauth_url.startsWith("https://slack.com/")) {
      globalThis.location.href = data.oauth_url
      return true
    }
  } catch {
    // Fall back to the normal landing; Connectors can still add Slack.
  }
  return false
}

export function useSlackSignInAvailable(enabled: boolean): boolean {
  const [available, setAvailable] = useState(false)
  useEffect(() => {
    if (!enabled || available) return
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch("/api/slack/signin-available")
        const data = res.ok ? await res.json() : null
        if (!cancelled) setAvailable(data?.available === true)
      } catch {
        // Hidden on a failed probe: a button that dead-ends is worse than none.
      }
    })()
    return () => { cancelled = true }
  }, [enabled, available])
  return available
}

function SlackButton({ href, label, busyLabel, disabled }: Readonly<{ href: string; label: string; busyLabel: string; disabled?: boolean }>) {
  const [busy, setBusy] = useState(false)
  return (
    <a
      href={href}
      onClick={(e) => { if (disabled) { e.preventDefault(); return } setBusy(true) }}
      aria-disabled={disabled}
      className={`flex w-full items-center justify-center gap-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] px-4 py-2.5 text-sm font-medium text-white transition-all duration-200 hover:bg-white/[0.06] focus:outline-none focus:ring-2 focus:ring-white/10 ${disabled ? "pointer-events-none opacity-50" : ""}`}
    >
      <Image src="/slack.png" alt="" width={16} height={16} className="h-4 w-4" />
      {busy ? busyLabel : label}
    </a>
  )
}

export function ContinueWithSlack({ disabled }: Readonly<{ disabled?: boolean }>) {
  return <SlackButton href={`${BACKEND_URL}/slack/signin/start`} label="Continue with Slack" busyLabel="Opening Slack…" disabled={disabled} />
}

export function AddAuroraToSlack() {
  return <SlackButton href={`${BACKEND_URL}/slack/signup/start`} label="Add Aurora to Slack" busyLabel="Opening Slack…" />
}

const HANDOFF_STEPS: Record<string, { title: string; desc: string; steps: string[] }> = {
  slack: {
    title: "Setting up Aurora",
    desc: "Slack is connected. This takes a second.",
    steps: ["Added Aurora to your workspace", "Created your Aurora account", "Signing you in"],
  },
  slack_join: {
    title: "Joining your team",
    desc: "Your workspace already uses Aurora.",
    steps: ["Slack confirmed who you are", "Added you to your team's Aurora", "Signing you in"],
  },
  slack_signin: {
    title: "Signing you in",
    desc: "Slack confirmed who you are.",
    steps: ["Slack confirmed who you are", "Signing you in"],
  },
}

const STEP_MS = 380

/** Replaces the form while a Slack handoff redeems: progress, not a login box. */
export function SlackHandoffPanel({ via }: Readonly<{ via: string | null }>) {
  const copy = HANDOFF_STEPS[via ?? ""] ?? HANDOFF_STEPS.slack_signin
  const reduce = useReducedMotion() ?? false
  const last = copy.steps.length - 1
  // The last step spins until the browser navigates away.
  const [shown, setShown] = useState(reduce ? last : 0)
  useEffect(() => {
    if (shown >= last) return
    const id = globalThis.setTimeout(() => setShown((n) => n + 1), STEP_MS)
    return () => globalThis.clearTimeout(id)
  }, [shown, last])

  return (
    <div className="space-y-8" aria-live="polite">
      <div>
        <h2 className="text-2xl font-semibold text-white">{copy.title}</h2>
        <p className="mt-2 text-sm text-[#888]">{copy.desc}</p>
      </div>
      <ol className="space-y-3.5">
        {copy.steps.map((step, i) => {
          if (i > shown) return null
          const pending = i === last
          return (
            <motion.li
              key={step}
              initial={reduce ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, ease: EASE }}
              className="flex items-center gap-3 text-sm"
            >
              {pending ? (
                <span className="h-[18px] w-[18px] rounded-full border-2 border-white/15 border-t-white/80 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
                  <circle cx="9" cy="9" r="9" fill={GREEN} />
                  <motion.path d="M5.2 9.4l2.4 2.4 5-5.3" fill="none" stroke="#0a0a0a" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
                    initial={reduce ? false : { pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ delay: 0.1, duration: 0.3, ease: EASE }} />
                </svg>
              )}
              <span className={pending ? "text-white" : "text-white/60"}>{step}</span>
            </motion.li>
          )
        })}
      </ol>
    </div>
  )
}

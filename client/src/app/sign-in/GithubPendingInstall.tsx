"use client"

// A brand-new GitHub installer lands here (?gh_new=<token>) before their
// account is created. Signed out, they go straight on to account creation
// with no click. Signed in to Aurora already, they are asked to connect the
// install to the org they are in, instead of getting a second account.

import { useEffect, useRef, useState } from "react"
import { signOut, useSession } from "next-auth/react"
import { getEnv } from "@/lib/env"

const BUTTON_PRIMARY = "w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"
const BUTTON_SECONDARY = "w-full py-2.5 px-4 rounded-lg border border-white/[0.12] text-white/80 text-sm hover:bg-white/[0.04] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"

const LINK_ERRORS: Record<string, string> = {
  CLAIM_INVALID: "This install link expired. Install Aurora again from Connectors.",
  CROSS_ORG_CLAIM_REFUSED: "That GitHub install is already connected to another Aurora organization.",
}

function completeUrl(token: string): string {
  const base = getEnv("NEXT_PUBLIC_BACKEND_URL") || ""
  return `${base}/github/app/signup/complete?t=${encodeURIComponent(token)}`
}

function Spinner() {
  return (
    <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>
  )
}

export function GithubPendingInstall({
  token,
  githubLogin,
  githubAccount,
}: Readonly<{ token: string; githubLogin: string | null; githubAccount: string | null }>) {
  const { data: session, status } = useSession()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const leaving = useRef(false)

  useEffect(() => {
    if (status !== "unauthenticated" || leaving.current) return
    leaving.current = true
    globalThis.location.replace(completeUrl(token))
  }, [status, token])

  if (status !== "authenticated") {
    return (
      <div className="space-y-8" aria-live="polite">
        <div>
          <h2 className="text-2xl font-semibold text-white">Setting up your account</h2>
          <p className="mt-2 text-[#888] text-sm">GitHub is connected. Creating your workspace and enrolling the repositories you selected.</p>
        </div>
        <div className="flex items-center gap-3 text-white/70 text-sm"><Spinner />Signing you in…</div>
      </div>
    )
  }

  const orgName = session?.user?.orgName || "your organization"
  const target = githubAccount || githubLogin || "GitHub"

  const connectHere = async () => {
    setBusy(true)
    setError("")
    try {
      const res = await fetch("/api/proxy/github/app/link-pending", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      })
      if (res.ok) {
        globalThis.location.href = "/connectors?installed=github"
        return
      }
      const body = await res.json().catch(() => null)
      const code = body && typeof body.code === "string" ? body.code : ""
      setError(LINK_ERRORS[code] ?? "GitHub could not be connected. Try again from Connectors.")
    } catch {
      setError("We couldn't reach Aurora. Please try again.")
    }
    setBusy(false)
  }

  const createSeparate = async () => {
    setBusy(true)
    leaving.current = true
    await signOut({ redirect: false })
    globalThis.location.replace(completeUrl(token))
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold text-white">Connect GitHub to {orgName}?</h2>
        <p className="mt-2 text-[#888] text-sm">
          You installed Aurora on <span className="text-white">{target}</span>
          {githubLogin ? <> as <span className="text-white">@{githubLogin}</span></> : null}.
          {" "}You&apos;re signed in to <span className="text-white">{orgName}</span>, so we can add it there and start reviewing its pull requests.
        </p>
      </div>
      {error && <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3"><p className="text-sm text-red-400">{error}</p></div>}
      <div className="space-y-3">
        <button type="button" className={BUTTON_PRIMARY} disabled={busy} onClick={connectHere}>
          {busy ? "Connecting…" : `Connect to ${orgName}`}
        </button>
        <button type="button" className={BUTTON_SECONDARY} disabled={busy} onClick={createSeparate}>
          Create a separate Aurora account
        </button>
      </div>
    </div>
  )
}

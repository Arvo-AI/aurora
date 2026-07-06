"use client"

import { useState, Suspense, useRef, useEffect } from "react"
import { signIn, useSession, signOut } from "next-auth/react"
import { useRouter, useSearchParams } from "next/navigation"
import Link from "next/link"
import Image from "next/image"
import dynamic from "next/dynamic"
import { useDarkPageBackground } from "@/hooks/useDarkPageBackground"

const AuroraShader = dynamic(() => import('@/app/components/AuroraShader'), { ssr: false })

type AuthMode = "signin" | "signup" | "verify-email" | "change-password"

const taglines: Record<AuthMode, { line1: string; line2: string; desc: string }> = {
  "signin": {
    line1: "The small hours belong to the sky,",
    line2: "We\u2019ll see to the rest.",
    desc: "AI-powered root cause analysis and remediation for modern infrastructure teams.",
  },
  "signup": {
    line1: "The night belongs to the sky,",
    line2: "We\u2019ll keep the watch.",
    desc: "Get your team up and running in minutes. Free tier includes 20 incidents per month.",
  },
  "verify-email": {
    line1: "Almost there,",
    line2: "Just one more step.",
    desc: "Check your inbox for a verification code to confirm your email address.",
  },
  "change-password": {
    line1: "New credentials,",
    line2: "Fresh start.",
    desc: "Your account was created by an admin. Please choose a new password to continue.",
  },
}

function getInitialMode(searchParams: URLSearchParams): AuthMode {
  const mode = searchParams.get("mode")
  if (mode === "signup") return "signup"
  if (mode === "verify-email") return "verify-email"
  if (mode === "change-password") return "change-password"
  return "signin"
}

function AuthPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { data: session, update } = useSession()
  const rawCallbackUrl = searchParams.get("callbackUrl") || "/"
  const callbackUrl =
    rawCallbackUrl.startsWith("/") && !rawCallbackUrl.startsWith("//")
      ? rawCallbackUrl
      : "/"

  const [mode, setMode] = useState<AuthMode>(() => getInitialMode(searchParams))
  const [formVisible, setFormVisible] = useState(true)
  const [taglineVisible, setTaglineVisible] = useState(true)
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [name, setName] = useState("")
  const [orgName, setOrgName] = useState("")
  const [error, setError] = useState("")
  const [isLoading, setIsLoading] = useState(false)
  const switching = useRef(false)

  // Verify-email state
  const [code, setCode] = useState("")
  const [resendCooldown, setResendCooldown] = useState(0)
  const [isResending, setIsResending] = useState(false)
  const [verified, setVerified] = useState(false)

  // Change-password state
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmNewPassword, setConfirmNewPassword] = useState("")

  useDarkPageBackground()

  useEffect(() => {
    const target = getInitialMode(searchParams)
    if (target !== mode && !switching.current) switchMode(target)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  useEffect(() => {
    if (resendCooldown <= 0) return
    const t = setTimeout(() => setResendCooldown(resendCooldown - 1), 1000)
    return () => clearTimeout(t)
  }, [resendCooldown])

  const switchMode = (newMode: AuthMode) => {
    if (switching.current) return
    switching.current = true
    setError("")
    setFormVisible(false)
    setTaglineVisible(false)
    const url = new URL(globalThis.location.href)
    if (newMode === "signin") url.searchParams.delete("mode")
    else url.searchParams.set("mode", newMode)
    globalThis.history.replaceState(null, "", url.toString())
    setTimeout(() => {
      setMode(newMode)
      setTimeout(() => {
        setFormVisible(true)
        setTaglineVisible(true)
        switching.current = false
      }, 50)
    }, 250)
  }

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault()
    setError("")
    setIsLoading(true)
    try {
      const result = await signIn("credentials", { email, password, redirect: false })
      if (result?.error) {
        setError("Invalid email or password")
      } else if (result?.ok) {
        router.push(callbackUrl)
        router.refresh()
      }
    } catch {
      setError("An error occurred. Please try again.")
    } finally {
      setIsLoading(false)
    }
  }

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault()
    setError("")
    if (password !== confirmPassword) { setError("Passwords do not match"); return }
    if (password.length < 8) { setError("Password must be at least 8 characters"); return }
    if (!orgName.trim()) { setError("Organization name is required"); return }
    if (orgName.trim().length > 100) { setError("Organization name must be 100 characters or less"); return }
    if (!/^[\w\s\-.,'&()]+$/u.test(orgName.trim())) {
      setError("Organization name can only contain letters, numbers, spaces, hyphens, periods, commas, apostrophes, ampersands, and parentheses")
      return
    }
    setIsLoading(true)
    try {
      const response = await fetch('/api/auth/register', {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, name, org_name: orgName.trim() })
      })
      if (!response.ok) {
        const data = await response.json()
        setError(data.error || "Registration failed")
        return
      }
      const result = await signIn("credentials", { email, password, redirect: false })
      if (result?.ok) {
        sessionStorage.removeItem("aurora_onboarding_state")
        sessionStorage.removeItem("aurora_onboarding_queue")
        sessionStorage.setItem("aurora_needs_onboarding", "true")
        const updated = await update()
        if (updated?.user?.emailVerified) {
          router.push("/onboarding")
        } else {
          switchMode("verify-email")
        }
      } else {
        setError("Registration successful but sign-in failed. Please sign in manually.")
      }
    } catch {
      setError("An error occurred. Please try again.")
    } finally {
      setIsLoading(false)
    }
  }

  const handleVerify = async () => {
    if (code.length !== 6) { setError("Please enter a valid 6-digit code"); return }
    setIsLoading(true)
    try {
      const res = await fetch("/api/auth/verify-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      })
      const data = await res.json()
      if (!res.ok && data.error !== "Email already verified") {
        setError(data.error || "Verification failed")
        return
      }
      setVerified(true)
      const dest = sessionStorage.getItem("aurora_needs_onboarding") ? "/onboarding" : "/"
      sessionStorage.removeItem("aurora_needs_onboarding")
      setTimeout(() => router.push(dest), 800)
    } catch {
      setError("An error occurred. Please try again.")
    } finally {
      setIsLoading(false)
    }
  }

  const handleResend = async () => {
    setIsResending(true)
    setError("")
    try {
      const res = await fetch("/api/auth/resend-verification", { method: "POST" })
      const data = await res.json()
      if (!res.ok) { setError(data.error || "Failed to resend code"); return }
      setResendCooldown(60)
    } catch {
      setError("Failed to resend code")
    } finally {
      setIsResending(false)
    }
  }

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault()
    setError("")
    if (newPassword.length < 8) { setError("New password must be at least 8 characters"); return }
    if (newPassword !== confirmNewPassword) { setError("Passwords do not match"); return }
    if (newPassword === currentPassword) { setError("New password must be different from your current password"); return }
    setIsLoading(true)
    try {
      const response = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ currentPassword, newPassword }),
      })
      const data = await response.json()
      if (!response.ok) { setError(data.error || "Failed to change password"); return }
      const userEmail = session?.user?.email
      if (!userEmail) { setError("Session expired. Please sign in again."); return }
      const result = await signIn("credentials", { email: userEmail, password: newPassword, redirect: false })
      if (result?.ok) {
        const updated = await update()
        if (updated?.user?.emailVerified) {
          router.push("/")
        } else {
          switchMode("verify-email")
        }
      } else {
        setError("Password changed but re-authentication failed. Please sign in again.")
      }
    } catch {
      setError("An error occurred. Please try again.")
    } finally {
      setIsLoading(false)
    }
  }

  const tagline = taglines[mode]
  let resendLabel = "Resend code"
  if (isResending) resendLabel = "Resending..."
  else if (resendCooldown > 0) resendLabel = `Resend in ${resendCooldown}s`

  return (
    <div className="flex h-screen bg-[#0a0a0a] relative overflow-hidden">
      <div className="absolute inset-0 overflow-hidden">
        <AuroraShader className="absolute inset-0 w-full h-full blur-[8px]" />
        <div className="absolute inset-0 bg-black/30" />
        <div className="absolute inset-0" style={{ background: 'linear-gradient(to bottom, transparent 0%, transparent 35%, rgba(0,0,0,0.35) 65%, rgba(0,0,0,0.7) 100%)' }} />
      </div>

      {/* Left panel */}
      <div className="hidden lg:flex lg:w-[55%] flex-col justify-between p-16 relative overflow-hidden">
        <div className="relative z-10">
          <div className="flex items-center gap-6">
            <Image src="/arvologo.png" alt="Aurora" width={80} height={80} className="rounded-2xl" />
            <div>
              <span className="text-white font-bold text-5xl tracking-tight">Aurora</span>
              <span className="text-white/40 text-xl ml-3 font-medium">by Arvo AI</span>
            </div>
          </div>
        </div>

        <div className="relative z-10 max-w-lg">
          <div className={`transition-all duration-250 ease-in-out ${taglineVisible ? 'opacity-100 translate-y-0' : 'opacity-0 -translate-y-2'}`}>
            <h1 className="text-5xl font-bold text-white leading-[1.15] tracking-tight">
              <span className="block whitespace-nowrap italic font-normal" style={{ fontFamily: 'Georgia, "Times New Roman", serif' }}>
                {tagline.line1}
              </span>
              <span className="block whitespace-nowrap text-transparent bg-clip-text bg-gradient-to-r from-[#7dd3fc] via-[#a78bfa] to-[#f472b6]">
                {tagline.line2}
              </span>
            </h1>
            <p className="text-white/50 text-xl leading-relaxed mt-6">{tagline.desc}</p>
          </div>
        </div>

        {mode === "signin" || mode === "signup" ? (
          <div className="relative z-10 space-y-5">
            <p className="text-white/25 text-xs uppercase tracking-[0.2em] font-medium">Trusted by</p>
            <div className="flex items-center gap-x-8 gap-y-3 flex-wrap h-8">
              <Image src="/google-logo-nobg.png" alt="Google" width={72} height={24} className="opacity-40 brightness-0 invert object-contain h-5 w-auto" priority />
              <Image src="/imedpharma-nobg.png" alt="I-MED Pharma" width={100} height={24} className="opacity-40 brightness-0 invert object-contain h-5 w-auto" priority />
              <Image src="/harbor-fab-nobg.png" alt="Harbor Fab" width={80} height={24} className="opacity-40 brightness-0 invert object-contain h-5 w-auto" priority />
              <Image src="/guzzonanoresearch-nobg.png" alt="Guzzo Nano Research" width={80} height={24} className="opacity-40 brightness-0 invert object-contain h-5 w-auto" priority />
              <Image src="/canoe-nobg.png" alt="Canoe Interpretation" width={80} height={24} className="opacity-40 brightness-0 invert object-contain h-5 w-auto" priority />
            </div>
            <div className="flex items-center gap-2 mt-4">
              <p className="text-white/20 text-xs">Backed by</p>
              <span className="text-white/40 text-xs font-medium">Panache Ventures</span>
              <span className="text-white/20">&middot;</span>
              <span className="text-white/40 text-xs font-medium">Front Row Ventures</span>
            </div>
          </div>
        ) : (
          <div className="relative z-10" />
        )}
      </div>

      {/* Right panel */}
      <div className="w-full lg:w-[45%] flex items-center justify-center p-8 bg-[#0a0a0a]/80 backdrop-blur-sm relative overflow-y-auto hide-scrollbar">
        <div className="w-full max-w-[360px]">
          <div className="lg:hidden flex flex-col items-center gap-3 mb-8">
            <Image src="/arvologo.png" alt="Aurora" width={48} height={48} className="rounded-xl" />
            <div className="text-center">
              <span className="text-white font-bold text-xl">Aurora</span>
              <p className="text-[#555] text-xs mt-1">by Arvo AI</p>
            </div>
          </div>

          <div className={`transition-opacity duration-300 ease-in-out ${formVisible ? 'opacity-100' : 'opacity-0'}`}>
            {mode === 'signin' && (
              <div className="space-y-8">
                <div>
                  <h2 className="text-2xl font-semibold text-white">Sign in</h2>
                  <p className="mt-2 text-[#888] text-sm">Welcome back. Enter your credentials to continue.</p>
                </div>
                <form className="space-y-4" onSubmit={handleSignIn}>
                  <div className="space-y-3">
                    <div>
                      <label htmlFor="signin-email" className="block text-xs font-medium text-[#888] mb-1.5">Email</label>
                      <input id="signin-email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="you@company.com" disabled={isLoading} />
                    </div>
                    <div>
                      <label htmlFor="signin-password" className="block text-xs font-medium text-[#888] mb-1.5">Password</label>
                      <input id="signin-password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Enter your password" disabled={isLoading} />
                    </div>
                  </div>
                  {error && <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3"><p className="text-sm text-red-400">{error}</p></div>}
                  <button type="submit" disabled={isLoading} className="w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200">
                    {isLoading ? <span className="flex items-center justify-center gap-2"><svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>Signing in...</span> : "Sign in"}
                  </button>
                </form>
                <p className="text-center text-sm text-[#555]">Don&apos;t have an account?{" "}<button onClick={() => switchMode('signup')} className="text-white/80 hover:text-white transition-colors">Sign up</button></p>
                <div className="pt-6 border-t border-white/[0.06]">
                  <p className="text-center text-xs text-[#444]">By signing in, you agree to our{" "}<Link href="/terms" className="text-[#666] hover:text-white/60 transition-colors">Terms</Link>{" "}and{" "}<Link href="/terms" className="text-[#666] hover:text-white/60 transition-colors">Privacy Policy</Link></p>
                </div>
              </div>
            )}

            {mode === 'signup' && (
              <div className="space-y-6">
                <div>
                  <h2 className="text-2xl font-semibold text-white">Create your workspace</h2>
                  <p className="mt-2 text-[#888] text-sm">Set up your organization and start resolving incidents.</p>
                </div>
                <form className="space-y-3" onSubmit={handleSignUp}>
                  <div>
                    <label htmlFor="org-name" className="block text-xs font-medium text-[#888] mb-1.5">Organization name</label>
                    <input id="org-name" type="text" required value={orgName} onChange={(e) => setOrgName(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Acme Inc." disabled={isLoading} />
                  </div>
                  <div>
                    <label htmlFor="signup-name" className="block text-xs font-medium text-[#888] mb-1.5">Full name</label>
                    <input id="signup-name" type="text" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Jane Smith" disabled={isLoading} />
                  </div>
                  <div>
                    <label htmlFor="signup-email" className="block text-xs font-medium text-[#888] mb-1.5">Work email</label>
                    <input id="signup-email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="jane@acme.com" disabled={isLoading} />
                  </div>
                  <div>
                    <label htmlFor="signup-password" className="block text-xs font-medium text-[#888] mb-1.5">Password</label>
                    <input id="signup-password" type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Min. 8 characters" disabled={isLoading} />
                  </div>
                  <div>
                    <label htmlFor="confirm-password" className="block text-xs font-medium text-[#888] mb-1.5">Confirm password</label>
                    <input id="confirm-password" type="password" autoComplete="new-password" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Confirm your password" disabled={isLoading} />
                  </div>
                  {error && <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3"><p className="text-sm text-red-400">{error}</p></div>}
                  <button type="submit" disabled={isLoading} className="w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200 mt-2">
                    {isLoading ? <span className="flex items-center justify-center gap-2"><svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>Creating workspace...</span> : "Create workspace"}
                  </button>
                </form>
                <p className="text-center text-sm text-[#555]">Already have an account?{" "}<button onClick={() => switchMode('signin')} className="text-white/80 hover:text-white transition-colors">Sign in</button></p>
                <div className="pt-4 border-t border-white/[0.06]">
                  <p className="text-center text-xs text-[#444]">By creating an account, you agree to our{" "}<Link href="/terms" className="text-[#666] hover:text-white/60 transition-colors">Terms</Link>{" "}and{" "}<Link href="/terms" className="text-[#666] hover:text-white/60 transition-colors">Privacy Policy</Link></p>
                </div>
              </div>
            )}

            {mode === 'verify-email' && (
              <div className="space-y-8">
                <div>
                  <h2 className="text-2xl font-semibold text-white">Verify your email</h2>
                  <p className="mt-2 text-[#888] text-sm">Enter the 6-digit code sent to {session?.user?.email || "your email"}</p>
                </div>
                <div className="space-y-4">
                  <div>
                    <label htmlFor="verify-code" className="block text-xs font-medium text-[#888] mb-1.5">Verification code</label>
                    <input id="verify-code" type="text" inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} onKeyDown={(e) => e.key === "Enter" && handleVerify()} className={`w-full px-3.5 py-2.5 rounded-lg border text-center text-2xl font-mono tracking-widest placeholder:text-[#555] focus:outline-none focus:ring-2 transition-colors duration-300 ${verified ? 'border-green-500/40 bg-green-500/10 text-green-400 focus:ring-green-500/20' : 'border-white/[0.12] bg-white/[0.03] text-white focus:ring-white/10 focus:border-white/20'}`} placeholder="000000" disabled={isLoading || verified} autoFocus />
                  </div>
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-[#555]">Code expires in 15 minutes</span>
                    <button type="button" onClick={handleResend} disabled={isResending || resendCooldown > 0} className="text-white/60 hover:text-white disabled:text-[#555] disabled:cursor-not-allowed transition-colors">
                      {resendLabel}
                    </button>
                  </div>
                  {error && !verified && <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3"><p className="text-sm text-red-400">{error}</p></div>}
                  <button type="button" onClick={handleVerify} disabled={isLoading || code.length !== 6 || verified} className="w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200">
                    {isLoading ? <span className="flex items-center justify-center gap-2"><svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>Verifying...</span> : "Verify"}
                  </button>
                </div>
                <p className="text-center text-sm text-[#555]">
                  <button onClick={async () => { await signOut({ redirect: false }); switchMode('signin') }} className="text-white/80 hover:text-white transition-colors inline-flex items-center gap-1.5">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
                    Back
                  </button>
                </p>
              </div>
            )}

            {mode === 'change-password' && (
              <div className="space-y-8">
                <div>
                  <h2 className="text-2xl font-semibold text-white">Set your password</h2>
                  <p className="mt-2 text-[#888] text-sm">Choose a new password to secure your account.</p>
                </div>
                <form className="space-y-4" onSubmit={handleChangePassword}>
                  <div className="space-y-3">
                    <div>
                      <label htmlFor="current-password" className="block text-xs font-medium text-[#888] mb-1.5">Current password</label>
                      <input id="current-password" type="password" autoComplete="current-password" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Password given by admin" disabled={isLoading} />
                    </div>
                    <div>
                      <label htmlFor="new-password" className="block text-xs font-medium text-[#888] mb-1.5">New password</label>
                      <input id="new-password" type="password" autoComplete="new-password" required value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Min. 8 characters" disabled={isLoading} />
                    </div>
                    <div>
                      <label htmlFor="confirm-new-password" className="block text-xs font-medium text-[#888] mb-1.5">Confirm new password</label>
                      <input id="confirm-new-password" type="password" autoComplete="new-password" required value={confirmNewPassword} onChange={(e) => setConfirmNewPassword(e.target.value)} className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20" placeholder="Confirm your new password" disabled={isLoading} />
                    </div>
                  </div>
                  {error && <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3"><p className="text-sm text-red-400">{error}</p></div>}
                  <button type="submit" disabled={isLoading} className="w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200">
                    {isLoading ? <span className="flex items-center justify-center gap-2"><svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" /></svg>Updating...</span> : "Set new password"}
                  </button>
                </form>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function SignInPage() {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center min-h-screen bg-[#0a0a0a]">
        <div className="w-6 h-6 border-2 border-white/20 border-t-white/80 rounded-full animate-spin" />
      </div>
    }>
      <AuthPage />
    </Suspense>
  )
}

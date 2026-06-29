"use client"

import { useState, useEffect } from "react"
import { useSession, signOut } from "next-auth/react"
import Image from "next/image"
import dynamic from "next/dynamic"
import { useDarkPageBackground } from "@/hooks/useDarkPageBackground"

const AuroraShader = dynamic(() => import('@/app/components/AuroraShader'), { ssr: false })

export default function VerifyEmailPage() {
  const { data: session, update } = useSession()
  const [code, setCode] = useState("")
  const [error, setError] = useState("")
  const [isLoading, setIsLoading] = useState(false)
  const [resendCooldown, setResendCooldown] = useState(0)
  const [isResending, setIsResending] = useState(false)
  const [ready, setReady] = useState(false)

  useDarkPageBackground()

  useEffect(() => {
    if (session?.user?.emailVerified) {
      window.location.href = "/onboarding"
    }
  }, [session?.user?.emailVerified])

  useEffect(() => {
    const t = setTimeout(() => setReady(true), 50)
    return () => clearTimeout(t)
  }, [])

  useEffect(() => {
    if (resendCooldown <= 0) return
    const t = setTimeout(() => setResendCooldown(resendCooldown - 1), 1000)
    return () => clearTimeout(t)
  }, [resendCooldown])

  const handleVerify = async () => {
    if (code.length !== 6) {
      setError("Please enter a valid 6-digit code")
      return
    }

    setError("")
    setIsLoading(true)

    try {
      const res = await fetch("/api/auth/verify-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      })
      const data = await res.json()

      if (!res.ok) {
        if (data.error === "Email already verified") {
          await update()
          window.location.href = "/onboarding"
          return
        }
        setError(data.error || "Verification failed")
        return
      }

      await update()
      window.location.href = "/onboarding"
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

      if (!res.ok) {
        setError(data.error || "Failed to resend code")
        return
      }
      setResendCooldown(60)
    } catch {
      setError("Failed to resend code")
    } finally {
      setIsResending(false)
    }
  }

  return (
    <div className="flex h-screen bg-[#0a0a0a] relative overflow-hidden">
      <div className="absolute inset-0 overflow-hidden">
        <AuroraShader className="absolute inset-0 w-full h-full blur-[8px]" />
        <div className="absolute inset-0 bg-black/30" />
        <div className="absolute inset-0" style={{ background: 'linear-gradient(to bottom, transparent 0%, transparent 35%, rgba(0,0,0,0.35) 65%, rgba(0,0,0,0.7) 100%)' }} />
      </div>

      {/* Left panel - branding */}
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

        <div className={`relative z-10 max-w-lg transition-all duration-300 ease-in-out ${ready ? 'opacity-100 translate-y-0' : 'opacity-0 -translate-y-2'}`}>
          <h1 className="text-5xl font-bold text-white leading-[1.15] tracking-tight">
            <span className="block whitespace-nowrap italic font-normal" style={{ fontFamily: 'Georgia, "Times New Roman", serif' }}>
              Almost there,
            </span>
            <span className="block whitespace-nowrap text-transparent bg-clip-text bg-gradient-to-r from-[#7dd3fc] via-[#a78bfa] to-[#f472b6]">
              Just one more step.
            </span>
          </h1>
          <p className="text-white/50 text-xl leading-relaxed mt-6">
            Check your inbox for a verification code to confirm your email address.
          </p>
        </div>

        <div className="relative z-10" />
      </div>

      {/* Right panel - verification form */}
      <div className="w-full lg:w-[45%] flex items-center justify-center p-8 bg-[#0a0a0a]/80 backdrop-blur-sm relative overflow-y-auto">
        <div className="w-full max-w-[360px]">
          <div className="lg:hidden flex flex-col items-center gap-3 mb-8">
            <Image src="/arvologo.png" alt="Aurora" width={48} height={48} className="rounded-xl" />
            <div className="text-center">
              <span className="text-white font-bold text-xl">Aurora</span>
              <p className="text-[#555] text-xs mt-1">by Arvo AI</p>
            </div>
          </div>

          <div className={`transition-opacity duration-300 ease-in-out ${ready ? 'opacity-100' : 'opacity-0'}`}>
            <div className="space-y-8">
              <div>
                <h2 className="text-2xl font-semibold text-white">Verify your email</h2>
                <p className="mt-2 text-[#888] text-sm">
                  Enter the 6-digit code sent to {session?.user?.email || "your email"}
                </p>
              </div>

              <div className="space-y-4">
                <div>
                  <label htmlFor="verify-code" className="block text-xs font-medium text-[#888] mb-1.5">Verification code</label>
                  <input
                    id="verify-code"
                    type="text"
                    inputMode="numeric"
                    maxLength={6}
                    value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                    onKeyDown={(e) => e.key === "Enter" && handleVerify()}
                    className="w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-center text-2xl font-mono tracking-widest placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20"
                    placeholder="000000"
                    disabled={isLoading}
                    autoFocus
                  />
                </div>

                <div className="flex items-center justify-between text-xs">
                  <span className="text-[#555]">Code expires in 15 minutes</span>
                  <button
                    type="button"
                    onClick={handleResend}
                    disabled={isResending || resendCooldown > 0}
                    className="text-white/60 hover:text-white disabled:text-[#555] disabled:cursor-not-allowed transition-colors"
                  >
                    {isResending
                      ? "Resending..."
                      : resendCooldown > 0
                        ? `Resend in ${resendCooldown}s`
                        : "Resend code"}
                  </button>
                </div>

                {error && (
                  <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3">
                    <p className="text-sm text-red-400">{error}</p>
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleVerify}
                  disabled={isLoading || code.length !== 6}
                  className="w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"
                >
                  {isLoading ? (
                    <span className="flex items-center justify-center gap-2">
                      <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                      </svg>
                      Verifying...
                    </span>
                  ) : "Verify"}
                </button>
              </div>

              <p className="text-center text-sm text-[#555]">
                <button
                  onClick={() => signOut({ callbackUrl: "/sign-in" })}
                  className="text-white/80 hover:text-white transition-colors inline-flex items-center gap-1.5"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M19 12H5M12 19l-7-7 7-7" />
                  </svg>
                  Back
                </button>
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

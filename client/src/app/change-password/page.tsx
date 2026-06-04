"use client"

import { useState } from "react"
import { useSession, signIn } from "next-auth/react"
import { AuroraShader } from "../components/AuroraShader"

export default function ChangePasswordPage() {
  const { data: session } = useSession()

  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [error, setError] = useState("")
  const [isLoading, setIsLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError("")

    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters")
      return
    }

    if (newPassword !== confirmPassword) {
      setError("Passwords do not match")
      return
    }

    if (newPassword === currentPassword) {
      setError("New password must be different from your current password")
      return
    }

    setIsLoading(true)

    try {
      const response = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ currentPassword, newPassword }),
      })

      const data = await response.json()

      if (!response.ok) {
        setError(data.error || "Failed to change password")
        return
      }

      // Re-authenticate with the new password to get a fresh JWT that
      // has mustChangePassword=false. Using updateSession() alone is
      // unreliable because the middleware may read the stale JWT cookie
      // before the update is fully persisted.
      await signIn("credentials", {
        email: session?.user?.email,
        password: newPassword,
        callbackUrl: "/",
      })
    } catch {
      setError("An error occurred. Please try again.")
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="relative min-h-screen bg-[#0a0a0a] flex items-center justify-center px-4 overflow-hidden">
      {/* Animated shader background */}
      <AuroraShader />

      {/* Dark overlay gradients */}
      <div className="absolute inset-0 bg-gradient-to-b from-[#0a0a0a]/60 via-transparent to-[#0a0a0a]/80" />
      <div className="absolute inset-0 bg-[#0a0a0a]/40" />

      {/* Card */}
      <div className="relative z-10 max-w-md w-full space-y-8 bg-white/[0.04] border border-white/[0.08] backdrop-blur-xl p-8 rounded-2xl">
        <div>
          <h2 className="text-center text-3xl font-bold text-white">
            Set your password
          </h2>
          <p className="mt-2 text-center text-sm text-[#888]">
            Your account was created by an admin. Please choose a new password to continue.
          </p>
        </div>
        <form className="mt-8 space-y-5" onSubmit={handleSubmit}>
          <div className="space-y-3">
            <div>
              <label htmlFor="current-password" className="sr-only">
                Current password
              </label>
              <input
                id="current-password"
                name="current-password"
                type="password"
                autoComplete="current-password"
                required
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                className="block w-full px-3 py-2.5 bg-white/[0.03] border border-white/[0.12] text-white placeholder:text-[#555] rounded-lg focus:outline-none focus:ring-1 focus:ring-white/20 focus:border-white/20 sm:text-sm transition-colors"
                placeholder="Current password (given by admin)"
                disabled={isLoading}
              />
            </div>
            <div>
              <label htmlFor="new-password" className="sr-only">
                New password
              </label>
              <input
                id="new-password"
                name="new-password"
                type="password"
                autoComplete="new-password"
                required
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className="block w-full px-3 py-2.5 bg-white/[0.03] border border-white/[0.12] text-white placeholder:text-[#555] rounded-lg focus:outline-none focus:ring-1 focus:ring-white/20 focus:border-white/20 sm:text-sm transition-colors"
                placeholder="New password (min 8 characters)"
                disabled={isLoading}
              />
            </div>
            <div>
              <label htmlFor="confirm-new-password" className="sr-only">
                Confirm new password
              </label>
              <input
                id="confirm-new-password"
                name="confirm-new-password"
                type="password"
                autoComplete="new-password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="block w-full px-3 py-2.5 bg-white/[0.03] border border-white/[0.12] text-white placeholder:text-[#555] rounded-lg focus:outline-none focus:ring-1 focus:ring-white/20 focus:border-white/20 sm:text-sm transition-colors"
                placeholder="Confirm new password"
                disabled={isLoading}
              />
            </div>
          </div>

          {error && (
            <div className="rounded-lg bg-red-500/10 border border-red-500/20 p-4">
              <p className="text-sm text-red-300">{error}</p>
            </div>
          )}

          <div>
            <button
              type="submit"
              disabled={isLoading}
              className="w-full flex justify-center py-2.5 px-4 bg-white text-black font-medium rounded-lg hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm"
            >
              {isLoading ? "Updating..." : "Set new password"}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

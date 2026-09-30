import { useState } from "react"
import { signIn } from "next-auth/react"

// Both reset endpoints are POST-JSON-and-read-a-message. Extracted so callers
// hold the state transitions rather than the plumbing, and so the error
// precedence (HTTP error > body error > fallback) is defined in one place.
async function postResetRequest(
  path: string,
  body: Record<string, string>,
  fallbackError: string,
): Promise<{ message?: string; error?: string }> {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
    const data = await response.json()
    if (!response.ok) return { error: data.error || fallbackError }
    return { message: data.message }
  } catch {
    return { error: "An error occurred. Please try again." }
  }
}

// Kept pure and outside the hook: reads as a plain list of rules.
export function validateResetForm(
  code: string,
  password: string,
  confirmation: string,
): string | null {
  if (code.length !== 6) return "Please enter the 6-digit code"
  if (password.length < 8) return "New password must be at least 8 characters"
  if (password !== confirmation) return "Passwords do not match"
  return null
}

type UsePasswordResetOptions = {
  /** Address the code is sent to; owned by the caller since sign-in shares it. */
  email: string
  /** New password fields are owned by the caller — change-password reuses them. */
  newPassword: string
  confirmNewPassword: string
  setError: (msg: string) => void
  setNotice: (msg: string) => void
  /** Resend cooldown is shared with the verify-email flow, so the caller owns it. */
  startCooldown: () => void
  /** A code was accepted for sending — move the user to the reset panel. */
  onCodeSent: () => void
  /** Reset succeeded; `signedIn` says whether the follow-up sign-in worked. */
  onResetComplete: (signedIn: boolean) => void
}

/**
 * Drives the "forgot password" → "enter code + new password" flow.
 *
 * Lives outside the sign-in page so that page stays a set of panels rather than
 * also owning three network handlers.
 */
export function usePasswordReset({
  email,
  newPassword,
  confirmNewPassword,
  setError,
  setNotice,
  startCooldown,
  onCodeSent,
  onResetComplete,
}: UsePasswordResetOptions) {
  const [resetCode, setResetCode] = useState("")
  const [resetSent, setResetSent] = useState(false)
  const [resetComplete, setResetComplete] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isResending, setIsResending] = useState(false)

  const requestCode = async () => {
    setError("")
    setNotice("")
    if (!email.trim()) {
      setError("Please enter your email")
      return
    }
    setIsSubmitting(true)
    const { message, error } = await postResetRequest(
      "/api/auth/forgot-password",
      { email: email.trim() },
      "Failed to send reset code",
    )
    setIsSubmitting(false)
    if (error) {
      setError(error)
      return
    }
    // The backend answers 200 with the same message whether or not the account
    // exists, so there is nothing to branch on here.
    setResetSent(true)
    setNotice(message || "If an account exists for that email, we've sent a reset code.")
    startCooldown()
    onCodeSent()
  }

  const submitReset = async () => {
    setError("")
    const validationError = validateResetForm(resetCode, newPassword, confirmNewPassword)
    if (validationError) {
      setError(validationError)
      return
    }
    setIsSubmitting(true)
    const { error } = await postResetRequest(
      "/api/auth/reset-password",
      { email: email.trim(), code: resetCode, newPassword },
      "Failed to reset password",
    )
    if (error) {
      setIsSubmitting(false)
      setError(error)
      return
    }
    setResetComplete(true)
    // Sign in with the password they just set rather than sending them back to a
    // form to retype it.
    const result = await signIn("credentials", {
      email: email.trim(),
      password: newPassword,
      redirect: false,
    })
    setIsSubmitting(false)
    onResetComplete(Boolean(result?.ok))
  }

  const resendCode = async () => {
    setIsResending(true)
    setError("")
    const { message, error } = await postResetRequest(
      "/api/auth/forgot-password",
      { email: email.trim() },
      "Failed to resend code",
    )
    setIsResending(false)
    if (error) {
      setError(error)
      return
    }
    setNotice(message || "A new code is on its way.")
    startCooldown()
  }

  return {
    resetCode,
    setResetCode,
    resetSent,
    resetComplete,
    isSubmitting,
    isResending,
    requestCode,
    submitReset,
    resendCode,
  }
}

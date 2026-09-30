"use client"

// The two panels of the forgot-password flow, lifted out of the sign-in page so
// that component stays a mode switch rather than also owning this markup.

const SPINNER = (
  <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
  </svg>
)

const BACK_ARROW = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M19 12H5M12 19l-7-7 7-7" />
  </svg>
)

const INPUT_CLASS =
  "w-full px-3.5 py-2.5 rounded-lg border border-white/[0.12] bg-white/[0.03] text-white text-sm placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/10 focus:border-white/20"
const LABEL_CLASS = "block text-xs font-medium text-[#888] mb-1.5"
const SUBMIT_CLASS =
  "w-full py-2.5 px-4 rounded-lg bg-white text-black text-sm font-medium hover:bg-white/90 focus:outline-none focus:ring-2 focus:ring-white/20 focus:ring-offset-2 focus:ring-offset-[#0a0a0a] disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-4 py-3">
      <p className="text-sm text-red-400">{message}</p>
    </div>
  )
}

function NoticeBox({ message }: { message: string }) {
  return (
    <div className="rounded-lg bg-white/[0.04] border border-white/[0.12] px-4 py-3" aria-live="polite">
      <p className="text-sm text-[#bbb]">{message}</p>
    </div>
  )
}

function BackToSignIn({ onBack }: { onBack: () => void }) {
  return (
    <p className="text-center text-sm text-[#555]">
      <button onClick={onBack} className="text-white/80 hover:text-white transition-colors inline-flex items-center gap-1.5">
        {BACK_ARROW}
        Back to sign in
      </button>
    </p>
  )
}

type ForgotPasswordPanelProps = {
  email: string
  setEmail: (v: string) => void
  error: string
  isSubmitting: boolean
  spamHint: string
  onSubmit: (e: React.FormEvent) => void
  onBack: () => void
}

export function ForgotPasswordPanel({
  email, setEmail, error, isSubmitting, spamHint, onSubmit, onBack,
}: ForgotPasswordPanelProps) {
  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-2xl font-semibold text-white">Forgot your password?</h2>
        <p className="mt-2 text-[#888] text-sm">Enter the email you signed up with and we&apos;ll send you a 6-digit reset code.</p>
        <p className="mt-2 text-[#666] text-xs">{spamHint}</p>
      </div>
      <form className="space-y-4" onSubmit={onSubmit}>
        <div>
          <label htmlFor="forgot-email" className={LABEL_CLASS}>Email</label>
          <input id="forgot-email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={INPUT_CLASS} placeholder="you@company.com" disabled={isSubmitting} />
        </div>
        {error && <ErrorBox message={error} />}
        <button type="submit" disabled={isSubmitting} className={SUBMIT_CLASS}>
          {isSubmitting ? <span className="flex items-center justify-center gap-2">{SPINNER}Sending code...</span> : "Send reset code"}
        </button>
      </form>
      <BackToSignIn onBack={onBack} />
    </div>
  )
}

type ResetPasswordPanelProps = {
  email: string
  setEmail: (v: string) => void
  resetCode: string
  setResetCode: (v: string) => void
  newPassword: string
  setNewPassword: (v: string) => void
  confirmNewPassword: string
  setConfirmNewPassword: (v: string) => void
  /** True once a code was requested from the previous panel. */
  resetSent: boolean
  /** True after a successful reset — freezes the form during the redirect. */
  resetComplete: boolean
  isSubmitting: boolean
  /** Precomputed by the caller, which also owns the shared resend cooldown. */
  resendDisabled: boolean
  resendLabel: string
  error: string
  notice: string
  spamHint: string
  onSubmit: (e: React.FormEvent) => void
  onResend: () => void
  onBack: () => void
}

export function ResetPasswordPanel({
  email, setEmail, resetCode, setResetCode, newPassword, setNewPassword,
  confirmNewPassword, setConfirmNewPassword, resetSent, resetComplete,
  isSubmitting, resendDisabled, resendLabel, error, notice, spamHint,
  onSubmit, onResend, onBack,
}: ResetPasswordPanelProps) {
  const frozen = isSubmitting || resetComplete
  const codeClass = resetComplete
    ? "border-green-500/40 bg-green-500/10 text-green-400 focus:ring-green-500/20"
    : "border-white/[0.12] bg-white/[0.03] text-white focus:ring-white/10 focus:border-white/20"

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-2xl font-semibold text-white">Choose a new password</h2>
        <p className="mt-2 text-[#888] text-sm">
          {resetSent
            ? `Enter the 6-digit code we sent to ${email || "your email"} and your new password.`
            : "Enter the 6-digit code from your email and your new password."}
        </p>
        <p className="mt-2 text-[#666] text-xs">{spamHint}</p>
      </div>
      <form className="space-y-4" onSubmit={onSubmit}>
        <div className="space-y-3">
          {/* Reachable directly via ?mode=reset-password, where no email was
              collected on the previous step — so keep it editable. */}
          <div>
            <label htmlFor="reset-email" className={LABEL_CLASS}>Email</label>
            <input id="reset-email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={INPUT_CLASS} placeholder="you@company.com" disabled={frozen} />
          </div>
          <div>
            <label htmlFor="reset-code" className={LABEL_CLASS}>Reset code</label>
            <input id="reset-code" type="text" inputMode="numeric" maxLength={6} required value={resetCode} onChange={(e) => setResetCode(e.target.value.replace(/\D/g, ""))} className={`w-full px-3.5 py-2.5 rounded-lg border text-center text-2xl font-mono tracking-widest placeholder:text-[#555] focus:outline-none focus:ring-2 transition-colors duration-300 ${codeClass}`} placeholder="000000" disabled={frozen} />
          </div>
          <div>
            <label htmlFor="reset-new-password" className={LABEL_CLASS}>New password</label>
            <input id="reset-new-password" type="password" autoComplete="new-password" required value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className={INPUT_CLASS} placeholder="Min. 8 characters" disabled={frozen} />
          </div>
          <div>
            <label htmlFor="reset-confirm-password" className={LABEL_CLASS}>Confirm new password</label>
            <input id="reset-confirm-password" type="password" autoComplete="new-password" required value={confirmNewPassword} onChange={(e) => setConfirmNewPassword(e.target.value)} className={INPUT_CLASS} placeholder="Confirm your new password" disabled={frozen} />
          </div>
        </div>
        <div className="flex items-center justify-between text-xs">
          <span className="text-[#555]">Code expires in 15 minutes</span>
          <button type="button" onClick={onResend} disabled={resendDisabled} className="text-white/60 hover:text-white disabled:text-[#555] disabled:cursor-not-allowed transition-colors">
            {resendLabel}
          </button>
        </div>
        {error && <ErrorBox message={error} />}
        {notice && !error && <NoticeBox message={notice} />}
        <button type="submit" disabled={frozen} className={SUBMIT_CLASS}>
          {isSubmitting ? <span className="flex items-center justify-center gap-2">{SPINNER}Resetting...</span> : "Reset password"}
        </button>
      </form>
      <BackToSignIn onBack={onBack} />
    </div>
  )
}

// Finishes a one-click GitHub install whose GitHub email already had an
// Aurora account. The backend sent the installer here with a signed claim
// token instead of logging them in; after they sign in, the token links the
// installation to their account.

const GITHUB_CLAIM_KEY = "aurora_github_pending_claim"

export type GithubClaimResult =
  | { status: "none" }
  | { status: "linked" }
  | { status: "failed"; message: string }

const FAILURE_MESSAGES: Record<string, string> = {
  CLAIM_INVALID: "The GitHub install link expired.",
  CLAIM_EMAIL_MISMATCH: "You signed in with a different email than your GitHub account uses.",
  CLAIM_IDENTITY_MISMATCH: "This account is linked to a different GitHub user.",
  CROSS_ORG_CLAIM_REFUSED: "That GitHub install is already connected to another Aurora organization.",
}

let inFlight: Promise<GithubClaimResult> | null = null

export function stashGithubClaim(token: string | null): void {
  if (!token) return
  try {
    sessionStorage.setItem(GITHUB_CLAIM_KEY, token)
  } catch {
    // Storage blocked: the Connectors page can still finish the link.
  }
}

function readClaim(): string | null {
  try {
    return sessionStorage.getItem(GITHUB_CLAIM_KEY)
  } catch {
    return null
  }
}

function dropClaim(): void {
  try {
    sessionStorage.removeItem(GITHUB_CLAIM_KEY)
  } catch {
    // Nothing to clean up.
  }
}

function failed(reason: string): GithubClaimResult {
  return {
    status: "failed",
    message: `You're signed in, but GitHub could not be connected. ${reason} Connect it from Connectors.`,
  }
}

async function redeem(token: string): Promise<GithubClaimResult> {
  let res: Response
  try {
    res = await fetch("/api/proxy/github/app/claim-pending", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    })
  } catch {
    // Network trouble: keep the token so a reload can retry.
    return failed("We couldn't reach Aurora.")
  }
  if (res.ok) {
    dropClaim()
    globalThis.location.href = "/connectors?installed=github"
    return { status: "linked" }
  }
  if (res.status >= 500) return failed("GitHub didn't respond.")
  // A refusal will not change on retry.
  dropClaim()
  const body = await res.json().catch(() => null)
  const code = body && typeof body.code === "string" ? body.code : ""
  return failed(FAILURE_MESSAGES[code] ?? "The link was refused.")
}

/** Links a stashed pending install. Concurrent callers share one attempt. */
export function resumeGithubClaim(): Promise<GithubClaimResult> {
  if (inFlight) return inFlight
  const token = readClaim()
  if (!token) return Promise.resolve({ status: "none" })
  inFlight = redeem(token).finally(() => {
    inFlight = null
  })
  return inFlight
}

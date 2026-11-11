// Finishes a one-click GitHub install whose GitHub email already had an
// Aurora account. The backend sent the installer here with a signed claim
// token instead of logging them in; after they sign in, the token links the
// installation to their account.

const GITHUB_CLAIM_KEY = "aurora_github_pending_claim"

export function stashGithubClaim(token: string | null): void {
  if (!token) return
  try {
    sessionStorage.setItem(GITHUB_CLAIM_KEY, token)
  } catch {
    // Storage blocked: the Connectors page can still finish the link.
  }
}

/** True when a pending install was linked and the browser is on its way to Connectors. */
export async function resumeGithubClaim(): Promise<boolean> {
  let token: string | null = null
  try {
    token = sessionStorage.getItem(GITHUB_CLAIM_KEY)
    sessionStorage.removeItem(GITHUB_CLAIM_KEY)
  } catch {
    return false
  }
  if (!token) return false
  try {
    const res = await fetch("/api/proxy/github/app/claim-pending", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    })
    if (!res.ok) return false
    globalThis.location.href = "/connectors?installed=github"
    return true
  } catch {
    return false
  }
}

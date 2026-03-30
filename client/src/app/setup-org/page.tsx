"use client"

import { useState, useEffect } from "react"
import { useSession } from "next-auth/react"

type ActiveTab = "create" | "join"

interface PendingInvitation {
  id: string
  orgName: string
  role: string
  invitedBy: string | null
  createdAt: string | null
}

export default function SetupOrgPage() {
  const { data: session } = useSession()
  const [activeTab, setActiveTab] = useState<ActiveTab>("create")

  const [orgName, setOrgName] = useState("")
  const [createError, setCreateError] = useState("")
  const [isCreating, setIsCreating] = useState(false)

  const [joinError, setJoinError] = useState("")
  const [joinSuccess, setJoinSuccess] = useState("")

  const [pendingInvitations, setPendingInvitations] = useState<PendingInvitation[]>([])
  const [loadingInvitations, setLoadingInvitations] = useState(true)
  const [acceptingId, setAcceptingId] = useState<string | null>(null)

  useEffect(() => {
    async function fetchInvitations() {
      try {
        const res = await fetch("/api/orgs/my-invitations")
        if (res.ok) {
          const data = await res.json()
          setPendingInvitations(data.invitations || [])
          if (data.invitations?.length > 0) {
            setActiveTab("join")
          }
        }
      } catch { /* ignore */ }
      setLoadingInvitations(false)
    }
    fetchInvitations()
  }, [])

  const handleAcceptInvitation = async (invitationId: string) => {
    setAcceptingId(invitationId)
    setJoinError("")

    try {
      const response = await fetch(`/api/orgs/my-invitations/${invitationId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "accept" }),
      })

      const data = await response.json()

      if (!response.ok) {
        setJoinError(data.error || "Failed to join organization")
        setAcceptingId(null)
        return
      }

      setJoinSuccess(`Successfully joined ${data.name || "the organization"}! Redirecting...`)
      window.location.replace("/org/switching")
    } catch {
      setJoinError("An error occurred. Please try again.")
      setAcceptingId(null)
    }
  }

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    setCreateError("")

    const trimmed = orgName.trim()
    if (!trimmed) {
      setCreateError("Organization name is required")
      return
    }

    if (trimmed.length > 100) {
      setCreateError("Organization name must be 100 characters or less")
      return
    }

    if (!/^[\w\s\-\.,'&()]+$/u.test(trimmed)) {
      setCreateError("Organization name can only contain letters, numbers, spaces, hyphens, periods, commas, apostrophes, ampersands, and parentheses")
      return
    }

    setIsCreating(true)

    try {
      const response = await fetch("/api/auth/setup-org", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ org_name: trimmed }),
      })

      const data = await response.json()

      if (!response.ok) {
        if (response.status === 404) {
          window.location.replace("/sign-in")
          return
        }
        if (response.status === 409 && data.code === "already_has_org") {
          window.location.replace("/org/switching")
          return
        }
        setCreateError(data.error || "Failed to create organization")
        setIsCreating(false)
        return
      }

      window.location.replace("/org/switching")
    } catch {
      setCreateError("An error occurred. Please try again.")
      setIsCreating(false)
    }
  }

  const userName = session?.user?.name

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-gray-50 to-gray-100 dark:from-gray-900 dark:to-black py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-lg w-full space-y-8">
        <div className="text-center">
          <h2 className="text-3xl font-extrabold text-gray-900 dark:text-white">
            Get started with Aurora
          </h2>
          <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
            Welcome{userName ? `, ${userName}` : ""}! Create a new organization or join an existing one.
          </p>
        </div>

        {/* Tab switcher */}
        <div className="flex rounded-lg bg-gray-100 dark:bg-gray-800 p-1">
          <button
            type="button"
            onClick={() => setActiveTab("create")}
            className={`flex-1 rounded-md py-2.5 text-sm font-medium transition-all ${
              activeTab === "create"
                ? "bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm"
                : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            }`}
          >
            Create Organization
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("join")}
            className={`flex-1 rounded-md py-2.5 text-sm font-medium transition-all ${
              activeTab === "join"
                ? "bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm"
                : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            }`}
          >
            Join Organization
          </button>
        </div>

        {/* Card */}
        <div className="bg-white dark:bg-gray-800 p-8 rounded-lg shadow-xl">
          {activeTab === "create" ? (
            <>
              <div className="mb-6">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                  Create a new organization
                </h3>
                <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                  Set up a fresh workspace for your team. You can always change the name later.
                </p>
              </div>
              <form className="space-y-6" onSubmit={handleCreate}>
                <div>
                  <label htmlFor="org-name" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Organization name
                  </label>
                  <input
                    id="org-name"
                    name="org-name"
                    type="text"
                    required
                    value={orgName}
                    onChange={(e) => setOrgName(e.target.value)}
                    className="appearance-none relative block w-full px-3 py-2 border border-gray-300 dark:border-gray-600 placeholder-gray-500 dark:placeholder-gray-400 text-gray-900 dark:text-white rounded-md focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm bg-white dark:bg-gray-700"
                    placeholder="e.g. Acme Corp"
                    autoFocus
                    disabled={isCreating}
                  />
                </div>

                {createError && (
                  <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-4">
                    <p className="text-sm text-red-800 dark:text-red-200">{createError}</p>
                  </div>
                )}

                <button
                  type="submit"
                  disabled={isCreating}
                  className="group relative w-full flex justify-center py-2.5 px-4 border border-transparent text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {isCreating ? "Creating organization..." : "Create Organization"}
                </button>
              </form>
            </>
          ) : (
            <>
              <div className="mb-6">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                  Join an existing organization
                </h3>
                <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                  Accept a pending invitation from your organization admin.
                </p>
              </div>

              {/* Pending invitations */}
              {!loadingInvitations && pendingInvitations.length > 0 && (
                <div className="mb-6 space-y-3">
                  <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    Pending invitations
                  </p>
                  {pendingInvitations.map((inv) => (
                    <div
                      key={inv.id}
                      className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-700/50"
                    >
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-gray-900 dark:text-white truncate">
                          {inv.orgName}
                        </p>
                        <p className="text-xs text-gray-500 dark:text-gray-400">
                          {inv.invitedBy ? `Invited by ${inv.invitedBy}` : "Invitation"} &middot; Role: {inv.role}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleAcceptInvitation(inv.id)}
                        disabled={acceptingId === inv.id || !!joinSuccess}
                        className="ml-3 flex-shrink-0 px-3 py-1.5 text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        {acceptingId === inv.id ? "Joining..." : "Accept"}
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {joinError && (
                <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-4 mb-4">
                  <p className="text-sm text-red-800 dark:text-red-200">{joinError}</p>
                </div>
              )}

              {joinSuccess && (
                <div className="rounded-md bg-green-50 dark:bg-green-900/20 p-4 mb-4">
                  <p className="text-sm text-green-800 dark:text-green-200">{joinSuccess}</p>
                </div>
              )}

              {!loadingInvitations && pendingInvitations.length === 0 && !joinSuccess && (
                <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-6">
                  No pending invitations. Ask your organization admin to send an invite to <span className="font-medium text-gray-700 dark:text-gray-200">{session?.user?.email || "your email"}</span>.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

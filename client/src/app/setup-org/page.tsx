"use client"

import { useState } from "react"
import { useSession, signOut } from "next-auth/react"

export default function SetupOrgPage() {
  const { data: session } = useSession()

  const [orgName, setOrgName] = useState("")
  const [error, setError] = useState("")
  const [isLoading, setIsLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError("")

    const trimmed = orgName.trim()
    if (!trimmed) {
      setError("Organization name is required")
      return
    }

    if (trimmed.length > 100) {
      setError("Organization name must be 100 characters or less")
      return
    }

    setIsLoading(true)

    try {
      const response = await fetch("/api/auth/setup-org", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ org_name: trimmed }),
      })

      const data = await response.json()

      if (!response.ok) {
        if (response.status === 404) {
          await signOut({ callbackUrl: "/sign-in" })
          return
        }
        setError(data.error || "Failed to create organization")
        setIsLoading(false)
        return
      }

      await signOut({ callbackUrl: "/sign-in" })
    } catch {
      setError("An error occurred. Please try again.")
      setIsLoading(false)
    }
  }

  const userName = session?.user?.name

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-gray-50 to-gray-100 dark:from-gray-900 dark:to-black py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full space-y-8 bg-white dark:bg-gray-800 p-8 rounded-lg shadow-xl">
        <div>
          <h2 className="mt-6 text-center text-3xl font-extrabold text-gray-900 dark:text-white">
            Create your organization
          </h2>
          <p className="mt-2 text-center text-sm text-gray-600 dark:text-gray-300">
            Welcome{userName ? `, ${userName}` : ""}! Set up an organization to get started with Aurora.
            You can always change the name later.
          </p>
        </div>
        <form className="mt-8 space-y-6" onSubmit={handleSubmit}>
          <div>
            <label htmlFor="org-name" className="sr-only">
              Organization name
            </label>
            <input
              id="org-name"
              name="org-name"
              type="text"
              required
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              className="appearance-none relative block w-full px-3 py-2 border border-gray-300 dark:border-gray-700 placeholder-gray-500 dark:placeholder-gray-400 text-gray-900 dark:text-white rounded-md focus:outline-none focus:ring-blue-500 focus:border-blue-500 focus:z-10 sm:text-sm bg-white dark:bg-gray-800"
              placeholder="Organization name"
              autoFocus
              disabled={isLoading}
            />
          </div>

          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-4">
              <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
            </div>
          )}

          <div>
            <button
              type="submit"
              disabled={isLoading}
              className="group relative w-full flex justify-center py-2 px-4 border border-transparent text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isLoading ? "Creating — you'll be asked to sign in again..." : "Create organization"}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
